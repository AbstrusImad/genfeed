# GenFeed

**Keyless, consensus-verified market data for [GenLayer](https://genlayer.com) Intelligent Contracts — the full Binance public API plus composite price primitives, in one line.**

Full, consensus-aware coverage of the **Binance public market-data API** — no API keys, no paid plans, no oracle subscriptions. Every reading is agreed upon by GenLayer validators before it touches your contract state:

```python
price  = BinanceConnector.get_price("BTCUSDT")          # u256 spot price (atto)
twap   = BinanceConnector.get_twap("BTCUSDT", "1h", 24)  # u256 24h TWAP (atto)
stats  = BinanceConnector.get_24h_stats("ETHUSDT")       # dict: high/low/volume/...
clock  = BinanceConnector.get_server_time()              # u256 epoch ms
```

[![GenVM](https://img.shields.io/badge/GenVM-validated_live-5ad17a?style=flat-square)](#validation--linting)
[![Runner](https://img.shields.io/badge/runner-pinned_hash-f5c542?style=flat-square)](#compatibility-verified-on-the-live-testnet)
[![APIs](https://img.shields.io/badge/APIs-100%25_free_·_no_keys-61dafb?style=flat-square)](#endpoint-coverage)

---

## Contents

- [Install & set up](#install--set-up) — add it to your project (no pip, no keys, no env)
- [Your first contract](#your-first-contract) — a working oracle in 3 steps, with deploy commands
- [Endpoint coverage](#endpoint-coverage) — every raw Binance public endpoint
- [Composite super-powers](#composite-super-powers) — median price, VWAP, volatility, peg checks, conversions…
- [Method reference](#method-reference) — conventions, parameters & defaults, exact return shapes
- [Oracle design guide](#choosing-the-right-reading-oracle-design-guide) — which reading to use, and when
- [Atto scale](#atto-scale-in-30-seconds) · [Error semantics](#error-semantics)
- [Deployment](#deployment) · [Validation, linting & live test](#validation--linting)
- [Compatibility notes](#compatibility-verified-on-the-live-testnet) · [Extending the library](#extending-the-library)

---

## Why this exists

Calling an external API from a blockchain is hard; calling one from a network where **multiple validators must independently agree on the result** is harder — especially when the data moves every millisecond. Every method in this library packages the full set of GenLayer consensus requirements so you don't have to re-derive them:

| Requirement | How the library handles it |
| --- | --- |
| **Non-deterministic isolation** | Every HTTP request runs exclusively inside a zero-argument closure handed to the GenVM — written *inline*, right next to the equivalence-principle call, so the non-deterministic surface is auditable at a glance. |
| **Per-endpoint consensus strategy** | Immutable data (closed candles, symbol metadata) → `gl.eq_principle.strict_eq`. Live data (prices, order book, stats) → custom validator (`gl.vm.run_nondet_unsafe`) in which **every validator independently re-fetches** and accepts only within an explicit tolerance. No rubber-stamping. |
| **No floats on-chain** | Decimals become **atto-scale integers** (value × 10¹⁸) via pure string/integer arithmetic — a Python `float` is never constructed, so results are bit-identical on every validator. |
| **Semantic error taxonomy** | HTTP 4xx → `[EXTERNAL]`, HTTP 5xx / connectivity → `[TRANSIENT]`, bad caller input → `[EXPECTED]` — all raised as `gl.vm.UserError`, so validator agreement is well-defined even on failure paths. |

---

## Install & set up

There is **nothing to `pip install`, no API key, and no environment variable.** The library is a single Python module that ships *inside* your contract.

**Prerequisites:** a GenLayer Intelligent-Contract project (see the [GenLayer docs](https://docs.genlayer.com)) and a deploy tool — the GenLayer CLI is easiest:

```bash
npm install -g genlayer
```

Then, three steps:

1. **Copy the module.** Put `genfeed.py` next to your contract `.py` file.
2. **Import it** in your contract: `from genfeed import BinanceConnector`.
3. **Bundle into one deployable file:** `python scripts/bundle.py` inlines the library into your contract and writes `dist/<contract>.bundle.py` — the single file you deploy. (GenLayer can also deploy multi-file packages with the `py-genlayer-multi` runner; see [Deployment](#deployment).)

Every method is a **static call on `BinanceConnector`** that runs the web request, reaches validator consensus, and converts decimals to atto for you — returning a `u256`, `dict`, or `list` you store directly. No client to instantiate, no keys to manage.

---

## Your first contract

A complete oracle: one write that fetches a consensus-validated price, one free view that reads it back.

```python
# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
from genlayer import *

from genfeed import BinanceConnector


class MyOracle(gl.Contract):
    btc_price_atto: u256              # storage slot — atto = price × 10**18

    @gl.public.write                  # a transaction: runs the web call + validator consensus
    def refresh(self) -> None:
        # One line does it all: HTTP isolation + agreement + atto conversion + error mapping.
        self.btc_price_atto = BinanceConnector.get_price("BTCUSDT")

    @gl.public.view                   # a free read — no transaction
    def price(self) -> str:
        return str(int(self.btc_price_atto))   # atto often exceeds JS safe-int range
```

Bundle it, then deploy and call with the GenLayer CLI:

```bash
python scripts/bundle.py                              # -> dist/myoracle.bundle.py
genlayer network set testnet-bradbury
genlayer deploy --contract dist/myoracle.bundle.py    # prints the new contract address
genlayer write <address> refresh                      # fetch + consensus → stores the price
genlayer call  <address> price                        # read it back (atto string)
```

The fully-worked, multi-method version (spot, TWAP, daily range, spread, median price, settle-if-safe…) lives in [`price_oracle.py`](price_oracle.py).

---

## Endpoint coverage

Everything the Binance public (key-less) REST API offers, mapped to a consensus-aware method:

| Binance endpoint | Method | Returns | Consensus strategy |
| --- | --- | --- | --- |
| `/ticker/price` | `get_price(symbol)` | `u256` spot price | `strict_eq` (byte-exact) |
| `/ticker/price` | `get_price_with_tolerance(symbol, bps=50)` | `u256` spot price | re-fetch + within bps |
| `/avgPrice` | `get_avg_price(symbol, bps=50)` | `u256` 5-min average | re-fetch + within bps |
| `/ticker/bookTicker` | `get_book_ticker(symbol, bps=50)` | `dict` bid/ask/mid/spread | re-fetch + bid & ask within bps |
| `/depth` | `get_order_book_summary(symbol, levels=500, ...)` | `dict` best prices + summed volumes | re-fetch + prices tight, summed volume coarse (deep book is stable; shallow is noisy) |
| `/ticker/24hr` | `get_24h_stats(symbol, bps=100)` | `dict` 12 fields (signed change, volumes, count) | re-fetch + field-wise, sign-aware bps |
| `/ticker` | `get_rolling_stats(symbol, window="1d", bps=100)` | `dict` same shape, custom window | re-fetch + field-wise bps |
| `/ticker/tradingDay` | `get_trading_day_stats(symbol, time_zone="0", bps=100)` | `dict` same shape, calendar-day window | re-fetch + field-wise bps |
| `/klines` | `get_klines(symbol, interval, limit, end_time_ms=0)` | `list[dict]` closed candles | `strict_eq` when `end_time_ms` pinned; immutable-candle overlap rule otherwise |
| `/klines` (derived) | `get_twap(symbol, interval, periods, bps=10)` | `u256` TWAP | re-fetch + within 10 bps |
| `/exchangeInfo?symbol=` | `get_symbol_info(symbol)` | `dict` status/base/quote | `strict_eq` (stable metadata) |
| `/time` | `get_server_time(tolerance_seconds=10)` | `u256` epoch ms | re-fetch + skew ≤ N s |

**Deliberately not exposed (and why):**

| Endpoint | Reason |
| --- | --- |
| `/ping` | Carries no data — meaningless under consensus. |
| `/trades`, `/aggTrades` | Per-trade streams cannot reach meaningful validator agreement (new trades land between any two fetches). Candles **are** aggregated trades — use `get_klines`. |
| `/historicalTrades` | Requires an API key — out of scope for a key-less library. |
| `/exchangeInfo` (full dump) | Multi-megabyte payload, unfit for validator compute budgets. The per-symbol form **is** exposed via `get_symbol_info`. |
| `/uiKlines` | UI-optimized duplicate of `/klines`. |

---

## Composite super-powers

Raw endpoints are just the substrate. These methods **combine several readings into a single higher-level financial primitive** — the things a contract actually wants to settle on. Each one issues **no direct non-deterministic call**: it composes the consensus-validated `get_*` methods above (every sub-call reaches its own agreement and returns the leader-agreed value to all nodes), then applies pure integer arithmetic — so the derived result is reproduced bit-for-bit on every validator.

| Method | Combines | Returns | What it gives you |
| --- | --- | --- | --- |
| `get_asset_price(asset, quote="USDT")` | `ticker/price` (direct **or** inverse market) | `u256` | Price *any* asset in *any* quote, even with no direct pair (auto-reciprocal). |
| `convert(from, to, amount_atto)` | 2× `get_asset_price` | `u256` | Value any token in any other, routed through USDT. |
| `get_median_price(asset)` | N× `ticker/price` (USDT/USDC/FDUSD) | `u256` | **Manipulation-resistant USD price** — median across independent markets; one bad market can't move it. |
| `value_basket({asset: amount_atto})` | N× `get_asset_price` | `u256` | Price a whole portfolio / index in one call. |
| `is_price_safe(symbol, max_bps=100)` | `price` + `avgPrice` + `TWAP` | `dict` | **Settle-safety guard**: True only if spot, 5-min avg and TWAP all agree — blocks wicks / depegs / manipulation. |
| `check_peg(asset, max_bps=50)` | `get_asset_price` | `dict` | Is a stablecoin within N bps of \$1? Returns the deviation. |
| `get_volatility(symbol, periods=24)` | `klines` | `dict` | Realized volatility (bps) from close-to-close returns — risk-based collateral / fees / insurance. |
| `get_trend(symbol, periods=24)` | `klines` | `dict` | `up` / `down` / `flat` vs the moving average — auto-resolve "did X move?" markets. |
| `get_liquidity_score(symbol)` | `depth` + `ticker/24hr` | `dict` | 0–100 market-health score (spread + depth + volume) before settling a large position. |
| `get_execution_price(symbol, side, size_atto)` | `depth` (walks the book) | `dict` | The **true VWAP** you'd pay/receive for a given size — not the headline top-of-book price. |
| `price_at(symbol, timestamp_ms)` | `klines` (pinned `endTime`) | `u256` | Byte-exact historical price at a past instant — settle options / bets at a fixed expiry. |

All atto/bps math is float-free and integer-only; signed fields (returns, price change, peg deviation) stay sign-aware. The number of underlying consensus sub-reads is bounded per method (median ≤ 8 markets, basket ≤ 20 legs) to keep per-transaction cost predictable.

```python
# Manipulation-resistant settlement price in one line:
usd = BinanceConnector.get_median_price("BTC")              # median(BTCUSDT, BTCUSDC, BTCFDUSD)

# Refuse to settle on a manipulated print:
report = BinanceConnector.is_price_safe("ETHUSDT")
if report["safe"]:
    settle(int(report["twap_atto"]))

# What 50 BTC would actually fetch right now (walking real depth):
fill = BinanceConnector.get_execution_price("BTCUSDT", "sell", 50 * 10**18)
```

---

## Method reference

### Conventions (apply to every method)

- **Symbols** are case-insensitive Binance market pairs (`"btcusdt"` → `"BTCUSDT"`); **assets** (for composites) are bare codes (`"BTC"`, `"USDT"`). Both are charset-validated before they ever touch a URL.
- **Prices and amounts are atto** — integers scaled by `10**18`. Divide by `10**18` for a human number; the library never constructs a float.
- **`tolerance_bps`** is how far a leader and a validator reading may differ and still agree, in basis points (`1 bp = 0.01%`, so `50` = 0.50%). Lower is stricter; `0` means byte-exact.
- **Signed fields** (24h change, returns, peg deviation) keep their sign, and agreement is sign-aware (+0.1% never agrees with −0.1%).
- **Big integers come back as strings** inside `dict` results (atto values routinely exceed JS safe-integer range); wrap them with `int(...)` before doing math.

### Parameters & defaults

| Method | Signature (defaults shown) |
| --- | --- |
| `get_price` | `get_price(symbol)` — byte-exact |
| `get_price_with_tolerance` | `(symbol, tolerance_bps=50)` |
| `get_avg_price` | `(symbol, tolerance_bps=50)` — Binance 5-min average |
| `get_book_ticker` | `(symbol, tolerance_bps=50)` |
| `get_order_book_summary` | `(symbol, levels=500, price_tolerance_bps=50, volume_tolerance_bps=5000)` |
| `get_24h_stats` | `(symbol, tolerance_bps=100)` |
| `get_rolling_stats` | `(symbol, window_size="1d", tolerance_bps=100)` — window `1m–59m`/`1h–23h`/`1d–7d` |
| `get_trading_day_stats` | `(symbol, time_zone="0", tolerance_bps=100)` — tz `"0"`, `"-5"`, `"5:45"` |
| `get_klines` | `(symbol, interval="1h", limit=24, end_time_ms=0)` — interval `1s…1M`; pin `end_time_ms` for `strict_eq` |
| `get_twap` | `(symbol, interval="1h", periods=24, tolerance_bps=10)` |
| `get_symbol_info` | `(symbol)` — metadata, `strict_eq` |
| `get_server_time` | `(tolerance_seconds=10)` |
| `get_asset_price` | `(asset, quote="USDT", tolerance_bps=50)` — direct or inverse market |
| `convert` | `(from_asset, to_asset, amount_atto, quote="USDT", tolerance_bps=50)` |
| `get_median_price` | `(asset, quotes=None, min_sources=2, tolerance_bps=50)` — quotes default `USDT/USDC/FDUSD` |
| `value_basket` | `(holdings, quote="USDT", tolerance_bps=50)` — `holdings` = `{asset: amount_atto}`, ≤ 20 legs |
| `is_price_safe` | `(symbol, max_divergence_bps=100)` |
| `check_peg` | `(asset, quote="USDT", target_atto=10**18, max_bps=50, tolerance_bps=50)` |
| `get_volatility` | `(symbol, interval="1h", periods=24)` |
| `get_trend` | `(symbol, interval="1h", periods=24, flat_band_bps=50)` |
| `get_liquidity_score` | `(symbol, levels=500)` |
| `get_execution_price` | `(symbol, side, base_amount_atto, levels=100, tolerance_bps=100)` — `side` = `"buy"`/`"sell"` |
| `price_at` | `(symbol, timestamp_ms, interval="1m")` — historical, `strict_eq` |

### Return shapes (the exact keys you get back)

Scalar methods return a `u256` in atto: `get_price*`, `get_avg_price`, `get_twap`, `get_asset_price`, `convert`, `get_median_price`, `value_basket`, `price_at`, `get_server_time`. The structured returns are:

```python
get_book_ticker        -> {"bid_atto", "ask_atto", "mid_atto", "spread_bps"}
get_order_book_summary -> {"best_bid_atto", "best_ask_atto", "bid_volume_atto", "ask_volume_atto", "levels"}
get_24h_stats          \
get_rolling_stats       > {"last_atto","open_atto","high_atto","low_atto","weighted_avg_atto",
get_trading_day_stats  /   "price_change_atto","price_change_percent_atto","base_volume_atto",
                           "quote_volume_atto","open_time_ms","close_time_ms","trade_count"}
get_klines             -> [ {"open_time_ms","open_atto","high_atto","low_atto","close_atto",
                             "base_volume_atto","close_time_ms","quote_volume_atto","trade_count"}, … ]
get_symbol_info        -> {"symbol","status","base_asset","quote_asset","is_spot_trading_allowed"}
is_price_safe          -> {"safe", "spot_atto", "avg_atto", "twap_atto", "max_divergence_bps"}
check_peg              -> {"pegged", "price_atto", "target_atto", "deviation_bps"}
get_volatility         -> {"volatility_bps", "mean_return_bps", "samples", "interval"}
get_trend              -> {"trend": "up"|"down"|"flat", "last_atto", "sma_atto", "deviation_bps"}
get_liquidity_score    -> {"score", "spread_bps", "depth_quote_atto", "volume_24h_quote_atto",
                           "components": {"spread", "depth", "volume"}}
get_execution_price    -> {"filled", "avg_price_atto", "filled_base_atto", "requested_base_atto",
                           "quote_cost_atto", "levels_used"}
```

---

## Choosing the right reading (oracle design guide)

- **Settlements & liquidations** → `get_twap`. Averaging closed candles makes the price far harder to manipulate than any instantaneous reading — this is the classic on-chain oracle primitive.
- **Live display / soft triggers** → `get_price_with_tolerance` or `get_avg_price`.
- **"Did BTC go up today?" markets** → `get_24h_stats` (`price_change_atto` is signed).
- **Liquidity checks before large settlements** → `get_order_book_summary` / `get_book_ticker`.
- **Reproducible historical evidence** → `get_server_time()` once, then pin `get_klines(..., end_time_ms=t)` — the window becomes fully deterministic and byte-exact across validators.

### How the tolerance methods reach consensus

The leader proposes a reading; each validator *independently re-fetches the same endpoint* and accepts only if both readings agree within the configured tolerance (integer-only math, sign-aware for fields that can be negative). The leader's value is what gets stored. Errors follow the canonical pattern: `[EXTERNAL]` must match exactly, two `[TRANSIENT]` failures agree, anything else forces leader rotation. The validator never rubber-stamps the leader's output — it always produces independent evidence.

---

## Atto scale in 30 seconds

Blockchains must not persist floats (hardware-dependent rounding breaks consensus). The standard fix — used across the EVM ecosystem — is fixed-point integers scaled by 10¹⁸ ("atto"):

| API answer | Stored on-chain |
| --- | --- |
| `"43250.12"` | `43250120000000000000000` |
| `"-1.25"` (24h change) | `-1250000000000000000` (signed int) |
| `"0.00000082"` | `820000000000` |

The conversion (`to_atto`) splits the decimal string and shifts digits — supports signs and scientific notation (`8.2e-08`), truncates beyond 18 decimals, range-checks against `u256`, and **never touches a float**. It is exported for reuse in your own connectors.

```python
from genfeed import to_atto
to_atto("43250.12")   # -> 43250120000000000000000
```

---

## Error semantics

All errors are `gl.vm.UserError` with an official GenLayer prefix — your frontend can pattern-match on them:

| Prefix | Raised when | Validator agreement rule |
| --- | --- | --- |
| `[EXPECTED]` | Invalid caller input (bad symbol charset, unknown interval, out-of-range tolerance) | Deterministic — same input, same error |
| `[EXTERNAL]` | HTTP 4xx, unknown symbol, malformed payload | Must match **exactly** |
| `[TRANSIENT]` | HTTP 5xx, empty body, non-JSON response | Two transient failures agree |

---

## Deployment

The library is a plain Python module; the example contract imports it. Two supported paths:

**1. Single-file bundle (recommended — verified against the live GenVM):**

```bash
python scripts/bundle.py        # -> dist/price_oracle.bundle.py
```

The bundle keeps the pinned runner header on line 1, inlines the library, and is accepted by any GenLayer deployment tool (GenLayer CLI, Studio, or a `genlayer-js` deploy script).

**2. Multi-file contract package:** keep `genfeed.py` alongside your contract and deploy with the `py-genlayer-multi` runner (see the [GenLayer docs](https://docs.genlayer.com) for packaging).

**Large-contract note (pubdata limit).** GenLayer stores a contract's source as rollup pubdata. The full, fully-documented bundle is ~72 KB, which can exceed a network's per-transaction pubdata budget (Bradbury rejects it with `BlockPubdataLimitReached` / `intrinsic gas too low`). Run the minifier to strip docstrings and comments — functionally identical, ~46% smaller (~39 KB), which deploys cleanly:

```bash
python scripts/bundle.py     # -> dist/price_oracle.bundle.py  (readable, canonical)
python scripts/minify.py     # -> dist/price_oracle.min.py     (deploy this when pubdata-limited)
genlayer deploy --contract dist/price_oracle.min.py
```

### Validation & linting

```bash
# Official AST linter (run on either bundle)
pip install genvm-linter
genvm-lint lint dist/price_oracle.bundle.py --json   # -> {"ok":true,"passed":3}

# Local live integration check: every method against the real Binance API,
# with an in-process GenVM shim that re-runs each validator (no deploy, free):
python tests/run_live.py                             # -> 25 passed, 0 failed

# Instant live-GenVM schema check (no deployment, free):
# genlayer-js getContractSchemaForCode(bundle)  (RPC gen_getContractSchema)
```

### Validated live on Bradbury

The minified bundle was deployed to GenLayer Bradbury Testnet and exercised end-to-end with real multi-validator consensus:

| Step | On-chain result |
| --- | --- |
| `deploy` | `ACCEPTED` / `AGREE` — contract created, constructor ran |
| `heartbeat()` → `get_server_time` | 5/5 validators `AGREE` |
| `update_robust_price("BTC")` → `get_median_price` (3 markets) | `AGREE`, stored `price_atto` |
| `settle_if_safe("BTCUSDT")` → `is_price_safe` (spot+avg+TWAP) | `AGREE`, stored `twap_atto` (only because the safety guard passed) |
| reads | `total_updates=3`, atto values match the live Binance reference |

---

## Compatibility (verified on the live testnet)

- **Pin the runner.** Contracts must start with `# { "Depends": "py-genlayer:<hash>" }`. The `test`/`latest` aliases are rejected by all public GenLayer networks.
- **Imports.** Recent SDK docs describe `import genlayer as gl` + `from genlayer.types import *`. On the currently pinned public-testnet runner, `genlayer.types` does **not** exist (`ModuleNotFoundError` — verified live). `from genlayer import *` exposes everything used here (`gl`, `u256`, `Address`, `TreeMap`, `DynArray`) and is what production deployments run today.
- **`web.get` over `web.render`.** `gl.nondet.web.get` returns a `Response` with `.status` and `.body`, which the `[EXTERNAL]`/`[TRANSIENT]` mapping requires. `render` returns only page text — status codes would be unrecoverable.
- **Region note.** Binance serves HTTP 451 from a few restricted regions. A region-blocked validator simply disagrees and consensus rotates the leader; it cannot corrupt state.

---

## Project layout

```
genfeed/
├── genfeed.py          # The library (importable module, no runner header)
├── price_oracle.py     # Example Intelligent Contract using the library
├── scripts/
│   ├── bundle.py       # Library + contract -> dist/price_oracle.bundle.py
│   └── minify.py       # Strip docstrings/comments -> dist/price_oracle.min.py
├── tests/
│   └── run_live.py     # Live integration check (real Binance + GenVM shim)
└── README.md
```

---

## Extending the library

Adding a connector for any other free API is mechanical — copy the Binance pattern:

1. **Validate inputs deterministically** (charset-restrict anything interpolated into the URL).
2. **Build the URL outside the closure**; put the `gl.nondet.web.get` call *inline* in a zero-argument closure, defined right next to the equivalence-principle call that runs it (this keeps the non-deterministic surface auditable and lint-traceable).
3. **Factor parsing into a deterministic helper** (status classification → `[EXTERNAL]`/`[TRANSIENT]`, JSON decode with `parse_float=str`, field extraction) and call it from the closure.
4. **Normalize to atto** with `to_atto` (strings in, integers out).
5. **Pick the consensus rule**: `strict_eq` for immutable/stable data; for live data, a custom `agree(theirs, mine)` passed to `_make_validator` under `gl.vm.run_nondet_unsafe`.
6. Raise only `gl.vm.UserError` with the official prefixes.

---

## Links

[GenLayer](https://genlayer.com) · [Documentation](https://docs.genlayer.com) · [SDK API reference](https://sdk.genlayer.com) · [GenLayer Skills](https://skills.genlayer.com/) · [Builders Portal](https://portal.genlayer.foundation/)
