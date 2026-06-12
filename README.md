# genlayer_free_connectors

**Zero-key, zero-cost external-data connectors for [GenLayer](https://genlayer.com) Intelligent Contracts.**

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

## Why this exists

Calling an external API from a blockchain is hard; calling one from a network where **multiple validators must independently agree on the result** is harder — especially when the data moves every millisecond. Every method in this library packages the full set of GenLayer consensus requirements so you don't have to re-derive them:

| Requirement | How the library handles it |
| --- | --- |
| **Non-deterministic isolation** | Every HTTP request runs exclusively inside a zero-argument closure handed to the GenVM — written *inline*, right next to the equivalence-principle call, so the non-deterministic surface is auditable at a glance. |
| **Per-endpoint consensus strategy** | Immutable data (closed candles, symbol metadata) → `gl.eq_principle.strict_eq`. Live data (prices, order book, stats) → custom validator (`gl.vm.run_nondet_unsafe`) in which **every validator independently re-fetches** and accepts only within an explicit tolerance. No rubber-stamping. |
| **No floats on-chain** | Decimals become **atto-scale integers** (value × 10¹⁸) via pure string/integer arithmetic — a Python `float` is never constructed, so results are bit-identical on every validator. |
| **Semantic error taxonomy** | HTTP 4xx → `[EXTERNAL]`, HTTP 5xx / connectivity → `[TRANSIENT]`, bad caller input → `[EXPECTED]` — all raised as `gl.vm.UserError`, so validator agreement is well-defined even on failure paths. |

---

## Quick start

```python
# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
from genlayer import *

from genlayer_free_connectors import BinanceConnector


class MyContract(gl.Contract):
    btc_price_atto: u256

    @gl.public.write
    def refresh(self) -> None:
        # One line: isolation + consensus + atto conversion + error mapping
        self.btc_price_atto = BinanceConnector.get_price("BTCUSDT")
```

A complete, deploy-ready example lives in [`price_oracle.py`](price_oracle.py).

---

## Endpoint coverage

Everything the Binance public (key-less) REST API offers, mapped to a consensus-aware method:

| Binance endpoint | Method | Returns | Consensus strategy |
| --- | --- | --- | --- |
| `/ticker/price` | `get_price(symbol)` | `u256` spot price | `strict_eq` (byte-exact) |
| `/ticker/price` | `get_price_with_tolerance(symbol, bps=50)` | `u256` spot price | re-fetch + within bps |
| `/avgPrice` | `get_avg_price(symbol, bps=50)` | `u256` 5-min average | re-fetch + within bps |
| `/ticker/bookTicker` | `get_book_ticker(symbol, bps=50)` | `dict` bid/ask/mid/spread | re-fetch + bid & ask within bps |
| `/depth` | `get_order_book_summary(symbol, levels=100, ...)` | `dict` best prices + summed volumes | re-fetch + prices/volumes within bps |
| `/ticker/24hr` | `get_24h_stats(symbol, bps=100)` | `dict` 12 fields (signed change, volumes, count) | re-fetch + field-wise, sign-aware bps |
| `/ticker` | `get_rolling_stats(symbol, window="1d", bps=100)` | `dict` same shape, custom window | re-fetch + field-wise bps |
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
from genlayer_free_connectors import to_atto
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

**2. Multi-file contract package:** keep `genlayer_free_connectors.py` alongside your contract and deploy with the `py-genlayer-multi` runner (see the [GenLayer docs](https://docs.genlayer.com) for packaging).

### Validation & linting

```bash
# Official AST linter
pip install genvm-linter
genvm-lint lint dist/price_oracle.bundle.py --json

# Instant live-GenVM schema check (no deployment, free):
# RPC gen_getContractSchema with the base64-encoded bundle
```

---

## Compatibility (verified on the live testnet)

- **Pin the runner.** Contracts must start with `# { "Depends": "py-genlayer:<hash>" }`. The `test`/`latest` aliases are rejected by all public GenLayer networks.
- **Imports.** Recent SDK docs describe `import genlayer as gl` + `from genlayer.types import *`. On the currently pinned public-testnet runner, `genlayer.types` does **not** exist (`ModuleNotFoundError` — verified live). `from genlayer import *` exposes everything used here (`gl`, `u256`, `Address`, `TreeMap`, `DynArray`) and is what production deployments run today.
- **`web.get` over `web.render`.** `gl.nondet.web.get` returns a `Response` with `.status` and `.body`, which the `[EXTERNAL]`/`[TRANSIENT]` mapping requires. `render` returns only page text — status codes would be unrecoverable.
- **Region note.** Binance serves HTTP 451 from a few restricted regions. A region-blocked validator simply disagrees and consensus rotates the leader; it cannot corrupt state.

---

## Project layout

```
genlayer_free_connectors/
├── genlayer_free_connectors.py   # The library (importable module, no runner header)
├── price_oracle.py               # Example Intelligent Contract using the library
├── scripts/
│   └── bundle.py                 # Library + contract -> dist/price_oracle.bundle.py
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
