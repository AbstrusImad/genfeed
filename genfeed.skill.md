# GenFeed — AI Skill

**GenFeed** is a GenLayer Intelligent Contract library that wraps the full Binance public API with consensus-aware primitives. No API keys, no oracle subscriptions — every reading is validated by multiple independent GenLayer validators before touching contract state.

**Library file:** copy `genfeed.py` from https://github.com/AbstrusImad/genfeed into your project.

---

## 1. Setup (required for every contract)

```python
# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
from genlayer import *
from genfeed import BinanceConnector
```

**Critical:** the `# { "Depends": ... }` line MUST be the first line of every contract. `py-genlayer:test` and `py-genlayer:latest` are rejected by all public networks. `from genlayer import *` is the only valid import form on the current testnet runner (`from genlayer.types import *` causes `ModuleNotFoundError`).

Bundle the library into a single deployable file before deploying:

```bash
python scripts/bundle.py      # -> dist/<contract>.bundle.py
python scripts/minify.py      # -> dist/<contract>.min.py  (required on Bradbury — pubdata limit)
genlayer deploy --contract dist/<contract>.min.py
```

---

## 2. All Methods — Signatures & Defaults

### Raw endpoints

| Method | Signature |
| --- | --- |
| `get_price` | `(symbol)` → `u256` atto — `strict_eq` (byte-exact) |
| `get_price_with_tolerance` | `(symbol, tolerance_bps=50)` → `u256` atto |
| `get_avg_price` | `(symbol, tolerance_bps=50)` → `u256` atto — Binance 5-min average |
| `get_book_ticker` | `(symbol, tolerance_bps=50)` → `dict` |
| `get_order_book_summary` | `(symbol, levels=500, price_tolerance_bps=50, volume_tolerance_bps=5000)` → `dict` |
| `get_24h_stats` | `(symbol, tolerance_bps=100)` → `dict` |
| `get_rolling_stats` | `(symbol, window_size="1d", tolerance_bps=100)` → `dict` — window `1m–59m`/`1h–23h`/`1d–7d` |
| `get_trading_day_stats` | `(symbol, time_zone="0", tolerance_bps=100)` → `dict` — tz `"0"`, `"-5"`, `"5:45"` |
| `get_klines` | `(symbol, interval="1h", limit=24, end_time_ms=0)` → `list[dict]` |
| `get_twap` | `(symbol, interval="1h", periods=24, tolerance_bps=10)` → `u256` atto |
| `get_symbol_info` | `(symbol)` → `dict` — `strict_eq` |
| `get_server_time` | `(tolerance_seconds=10)` → `u256` epoch ms |

### Composite super-powers (compose multiple raw calls — no direct HTTP inside)

| Method | Signature |
| --- | --- |
| `get_asset_price` | `(asset, quote="USDT", tolerance_bps=50)` → `u256` — auto-reciprocal if no direct pair |
| `convert` | `(from_asset, to_asset, amount_atto, quote="USDT", tolerance_bps=50)` → `u256` |
| `get_median_price` | `(asset, quotes=None, min_sources=2, tolerance_bps=50)` → `u256` — quotes default: USDT/USDC/FDUSD |
| `value_basket` | `(holdings, quote="USDT", tolerance_bps=50)` → `u256` — `holdings = {"BTC": amount_atto, ...}`, ≤ 20 legs |
| `is_price_safe` | `(symbol, max_divergence_bps=100)` → `dict` — spot + avg + TWAP convergence guard |
| `check_peg` | `(asset, quote="USDT", target_atto=10**18, max_bps=50, tolerance_bps=50)` → `dict` |
| `get_volatility` | `(symbol, interval="1h", periods=24)` → `dict` — realized vol, float-free |
| `get_trend` | `(symbol, interval="1h", periods=24, flat_band_bps=50)` → `dict` |
| `get_liquidity_score` | `(symbol, levels=500)` → `dict` — 0-100 market health score |
| `get_execution_price` | `(symbol, side, base_amount_atto, levels=100, tolerance_bps=100)` → `dict` — `side="buy"/"sell"` |
| `price_at` | `(symbol, timestamp_ms, interval="1m")` → `u256` — historical, `strict_eq`, MUST be a past closed candle |

**Symbols** (`"BTCUSDT"`, `"ETHUSDT"`) are case-insensitive Binance pairs. **Assets** (`"BTC"`, `"ETH"`) are bare codes used by composite methods.

---

## 3. Return Shapes

Scalar methods return a `u256` in atto: `get_price*`, `get_avg_price`, `get_twap`, `get_asset_price`, `convert`, `get_median_price`, `value_basket`, `price_at`, `get_server_time`.

Structured returns — exact keys:

```python
get_book_ticker        -> {"bid_atto", "ask_atto", "mid_atto", "spread_bps"}

get_order_book_summary -> {"best_bid_atto", "best_ask_atto", "bid_volume_atto",
                           "ask_volume_atto", "levels"}

# Same shape for get_24h_stats, get_rolling_stats, get_trading_day_stats:
stats_dict             -> {"last_atto", "open_atto", "high_atto", "low_atto",
                           "weighted_avg_atto", "price_change_atto",
                           "price_change_percent_atto", "base_volume_atto",
                           "quote_volume_atto", "open_time_ms", "close_time_ms",
                           "trade_count"}

get_klines             -> [{"open_time_ms", "open_atto", "high_atto", "low_atto",
                            "close_atto", "base_volume_atto", "close_time_ms",
                            "quote_volume_atto", "trade_count"}, ...]

get_symbol_info        -> {"symbol", "status", "base_asset", "quote_asset",
                           "is_spot_trading_allowed"}

is_price_safe          -> {"safe": bool, "spot_atto", "avg_atto", "twap_atto",
                           "max_divergence_bps"}

check_peg              -> {"pegged": bool, "price_atto", "target_atto",
                           "deviation_bps"}

get_volatility         -> {"volatility_bps", "mean_return_bps", "samples",
                           "interval"}

get_trend              -> {"trend": "up"|"down"|"flat", "last_atto", "sma_atto",
                           "deviation_bps"}

get_liquidity_score    -> {"score", "spread_bps", "depth_quote_atto",
                           "volume_24h_quote_atto",
                           "components": {"spread", "depth", "volume"}}

get_execution_price    -> {"filled": bool, "avg_price_atto", "filled_base_atto",
                           "requested_base_atto", "quote_cost_atto", "levels_used"}
```

**Big integers come back as strings** inside dict results (atto values routinely exceed JS safe-integer range). Wrap with `int(...)` before math: `int(stats["high_atto"])`.

---

## 4. Atto Scale

All prices and amounts are **atto integers** = `value × 10**18`. The library NEVER constructs a Python float; all conversions use string/integer arithmetic.

```python
from genfeed import ATTO_SCALE, to_atto

ATTO_SCALE = 10**18

to_atto("43250.12")   # -> 43250120000000000000000  (u256)
to_atto("-1.25")      # -> -1250000000000000000     (signed int, for change fields)
to_atto("8.2e-08")    # -> 82000000000              (scientific notation supported)

# Human-readable display (off-chain only):
human = int(price_atto) / ATTO_SCALE
```

When storing atto values in contract state, always cast to `u256`:

```python
self.price_atto = u256(int(BinanceConnector.get_price("BTCUSDT")))
```

---

## 5. Error Handling

All errors are `gl.vm.UserError` with a prefix. Match on the prefix for consensus:

| Prefix | Meaning | Consensus rule |
| --- | --- | --- |
| `[EXPECTED]` | Bad caller input (invalid symbol, bad interval) | Deterministic — same input = same error |
| `[EXTERNAL]` | HTTP 4xx, unknown symbol, bad payload | Must match **exactly** |
| `[TRANSIENT]` | HTTP 5xx, empty body, network issue | Two transient failures agree |

```python
from genfeed import ERROR_EXPECTED, ERROR_EXTERNAL, ERROR_TRANSIENT

try:
    price = BinanceConnector.get_price("FAKEUSDT")
except gl.vm.UserError as e:
    if e.message.startswith(ERROR_EXTERNAL):
        raise gl.vm.UserError(f"{ERROR_EXPECTED} Unknown symbol")
```

---

## 6. Consensus Rules — Critical

- **`strict_eq`** is used for immutable/stable data: `get_price` (snapshot), `get_symbol_info`, `price_at` (pinned timestamp). Byte-exact across all validators.
- **Custom tolerance validator** (`gl.vm.run_nondet_unsafe`) is used for live data: every validator independently re-fetches and accepts only if within `tolerance_bps`. The leader's value is stored; the validator never rubber-stamps.
- **You never call `gl.nondet.web.get` yourself** — the library does this internally. Just call the static methods.
- **Composite methods issue no HTTP directly** — they chain `get_*` methods. Each sub-call has its own consensus block. The final derived result is bit-identical across validators because it is pure integer math on already-agreed inputs.

---

## 7. Oracle Design Guide

| Use case | Best method |
| --- | --- |
| Settlement / liquidation | `get_twap` — hardest to manipulate |
| Manipulation-resistant USD price | `get_median_price` — median across N markets |
| Pre-settlement safety check | `is_price_safe` — blocks wicks/depegs |
| Live display / soft triggers | `get_price_with_tolerance` or `get_avg_price` |
| Stablecoin peg verification | `check_peg` |
| "Did price move?" markets | `get_trend` or `get_24h_stats` (`price_change_atto` is signed) |
| Portfolio value | `value_basket` |
| Large-order true cost | `get_execution_price` — walks real order book depth |
| Option/bet historical expiry | `price_at(symbol, timestamp_ms)` — MUST be past, closed candle |
| Decentralized clock | `get_server_time` |

---

## 8. Example Contract

```python
# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
from genlayer import *
from genfeed import BinanceConnector, ERROR_EXPECTED


class PriceOracle(gl.Contract):
    owner: Address
    prices_atto: TreeMap[str, u256]
    total_updates: u256

    def __init__(self):
        self.owner = gl.message.sender_address
        self.total_updates = u256(0)

    @gl.public.write
    def update_price(self, symbol: str) -> None:
        key = symbol.strip().upper()
        self.prices_atto[key] = BinanceConnector.get_price(symbol)
        self.total_updates = u256(int(self.total_updates) + 1)

    @gl.public.write
    def settle_if_safe(self, symbol: str) -> None:
        """Refuses to settle on manipulated/wicked prices."""
        report = BinanceConnector.is_price_safe(symbol)
        if not report["safe"]:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} Unsafe to settle: {report['max_divergence_bps']}bps"
            )
        key = symbol.strip().upper()
        self.prices_atto[key] = u256(int(report["twap_atto"]))
        self.total_updates = u256(int(self.total_updates) + 1)

    @gl.public.view
    def get_price(self, symbol: str) -> str:
        key = symbol.strip().upper()
        price = self.prices_atto.get(key)
        if price is None:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} No stored price for '{key}'")
        return str(int(price))   # string — atto exceeds JS safe-int range
```

---

## 9. Critical Rules (will break consensus if violated)

1. **No Python floats** — never use `/`, `math.*`, or `float()` on price data. Use atto integers and integer division `//`.
2. **No direct `gl.nondet.web.get` calls** inside your contract when using GenFeed — the library handles all HTTP isolation. Call the static methods only.
3. **`price_at` requires a past, closed candle.** Passing the current or future timestamp makes the kline non-deterministic; validators will disagree.
4. **Always use `u256(int(...))` when storing atto values** from dict results — dict values are strings.
5. **`from genlayer import *` only.** `from genlayer.types import *` causes `ModuleNotFoundError` on the current testnet runner.
6. **Pinned runner hash on line 1.** No `py-genlayer:test`, no `py-genlayer:latest`.
7. **Never import `genfeed` inside the contract on Bradbury** — bundle first (`scripts/bundle.py`) then minify (`scripts/minify.py`) to fit the pubdata limit (~39 KB).

---

## 10. Deployment Workflow

```bash
# 1. Bundle: inline genfeed.py into your contract
python scripts/bundle.py         # -> dist/<contract>.bundle.py

# 2. Minify: strip docstrings/comments to fit Bradbury's pubdata limit
python scripts/minify.py         # -> dist/<contract>.min.py  (~46% smaller)

# 3. Lint (optional but recommended)
pip install genvm-linter
genvm-lint lint dist/<contract>.min.py --json   # must return {"ok":true}

# 4. Deploy
genlayer network set testnet-bradbury
genlayer deploy --contract dist/<contract>.min.py

# 5. Interact
genlayer write <address> update_price --args "BTCUSDT"
genlayer call  <address> get_price    --args "BTCUSDT"
```

**Local live test** (all 22 methods against real Binance, no deploy needed):

```bash
python tests/run_live.py   # -> 25 passed, 0 failed
```

---

## 11. Exported Constants

```python
from genfeed import ATTO_SCALE, to_atto, ERROR_EXPECTED, ERROR_EXTERNAL, ERROR_TRANSIENT
from genfeed import BinanceConnector

ATTO_SCALE = 10**18
```

---

Source: https://github.com/AbstrusImad/genfeed
