#!/usr/bin/env python3
"""Local live integration check for GenFeed.

Runs every BinanceConnector method against the **real** Binance public API
using a minimal in-process GenVM shim. The shim is faithful where it matters:

  * ``gl.nondet.web.get`` performs a real HTTP GET (exposing ``.status`` /
    ``.body`` exactly like the runner).
  * ``gl.vm.run_nondet_unsafe(leader, validator)`` runs the leader, then
    **independently re-runs the validator** (which re-fetches and applies the
    agreement rule) — so the consensus path is genuinely exercised, not faked.
  * ``gl.eq_principle.strict_eq`` runs the deterministic closure.

This validates fetch + parse + atto math + consensus-agreement logic. It does
NOT replace a real multi-validator deployment (that is the separate Bradbury
deploy test), but it catches arithmetic/agreement bugs in seconds and for free.

Usage:  python tests/run_live.py
"""

import os
import sys
import types
import time
import urllib.request
import urllib.error

# --------------------------------------------------------------------------
# Minimal GenVM shim — injected as the `genlayer` module before importing lib
# --------------------------------------------------------------------------

_gl_mod = types.ModuleType("genlayer")


class UserError(Exception):
    def __init__(self, message=""):
        super().__init__(message)
        self.message = message


class Return:
    def __init__(self, calldata):
        self.calldata = calldata


class _Resp:
    def __init__(self, status, body):
        self.status = int(status)
        self.body = body  # bytes, like the runner


def _web_get(url, headers=None):
    h = dict(headers or {})
    h.setdefault("User-Agent", "Mozilla/5.0 (genlayer-free-connectors live test)")
    req = urllib.request.Request(url, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return _Resp(r.status, r.read())
    except urllib.error.HTTPError as e:  # 4xx/5xx still carry a status+body
        return _Resp(e.code, e.read() or b"")


REFETCHES = {"n": 0}


def _strict_eq(fetch):
    return fetch()


def _run_nondet_unsafe(leader_fn, validator_fn):
    """Leader runs; validator independently re-runs and must agree."""
    result = leader_fn()
    REFETCHES["n"] += 1
    if not validator_fn(Return(result)):
        raise UserError("[TEST] validator disagreed with leader (real consensus would rotate the leader)")
    return result


def _u256(x):
    v = int(x)
    if v < 0 or v > 2**256 - 1:
        raise UserError(f"[TEST] u256 out of range: {v}")
    return v


class _Generic:
    def __class_getitem__(cls, item):
        return cls


class Address(_Generic):
    ...


class TreeMap(_Generic):
    ...


class DynArray(_Generic):
    ...


_gl = types.SimpleNamespace(
    vm=types.SimpleNamespace(UserError=UserError, Return=Return, run_nondet_unsafe=_run_nondet_unsafe),
    nondet=types.SimpleNamespace(web=types.SimpleNamespace(get=_web_get)),
    eq_principle=types.SimpleNamespace(strict_eq=_strict_eq),
    message=types.SimpleNamespace(sender_address=None),
    Contract=object,
)

_gl_mod.gl = _gl
_gl_mod.u256 = _u256
_gl_mod.Address = Address
_gl_mod.TreeMap = TreeMap
_gl_mod.DynArray = DynArray
_gl_mod.__all__ = ["gl", "u256", "Address", "TreeMap", "DynArray"]
sys.modules["genlayer"] = _gl_mod

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import genfeed as fc  # noqa: E402

B = fc.BinanceConnector
ONE = fc.ATTO_SCALE


def human(atto):
    return int(atto) / ONE


# --------------------------------------------------------------------------
# Test runner
# --------------------------------------------------------------------------

PASS = 0
FAIL = 0
results = []


def check(name, fn, verify):
    global PASS, FAIL
    try:
        out = fn()
        ok, detail = verify(out)
        if ok:
            PASS += 1
            results.append(("PASS", name, detail))
        else:
            FAIL += 1
            results.append(("FAIL", name, f"assertion failed -> {detail}"))
    except Exception as e:  # noqa: BLE001
        FAIL += 1
        results.append(("FAIL", name, f"{type(e).__name__}: {e}"))


# Cache a couple of spot prices for cross-checks.
btc = int(B.get_price("BTCUSDT"))
eth = int(B.get_price("ETHUSDT"))

# ---- raw endpoints ----
check("get_price(BTCUSDT) strict_eq", lambda: B.get_price("BTCUSDT"),
      lambda r: (int(r) > 0, f"${human(r):,.2f}"))

check("get_price_with_tolerance(BTCUSDT)", lambda: B.get_price_with_tolerance("BTCUSDT"),
      lambda r: (int(r) > 0, f"${human(r):,.2f}"))

check("get_avg_price(BTCUSDT)", lambda: B.get_avg_price("BTCUSDT"),
      lambda r: (int(r) > 0, f"${human(r):,.2f}"))

check("get_book_ticker(BTCUSDT)", lambda: B.get_book_ticker("BTCUSDT"),
      lambda r: (r["bid_atto"] <= r["ask_atto"] and r["spread_bps"] >= 0,
                 f"spread={r['spread_bps']}bps mid=${human(r['mid_atto']):,.2f}"))

check("get_order_book_summary(BTCUSDT) [levels=500]", lambda: B.get_order_book_summary("BTCUSDT"),
      lambda r: (r["bid_volume_atto"] > 0 and r["ask_volume_atto"] > 0,
                 f"levels={r['levels']} bidVol={human(r['bid_volume_atto']):.2f} BTC"))

check("get_24h_stats(BTCUSDT)", lambda: B.get_24h_stats("BTCUSDT"),
      lambda r: (r["high_atto"] >= r["low_atto"] > 0,
                 f"chg={human(r['price_change_percent_atto']):.2f}% vol={human(r['quote_volume_atto']):,.0f}"))

check("get_rolling_stats(BTCUSDT,1d)", lambda: B.get_rolling_stats("BTCUSDT", "1d"),
      lambda r: (r["high_atto"] >= r["low_atto"] > 0, f"last=${human(r['last_atto']):,.2f}"))

check("get_trading_day_stats(BTCUSDT,0) [NEW]", lambda: B.get_trading_day_stats("BTCUSDT", "0"),
      lambda r: (r["high_atto"] >= r["low_atto"] > 0,
                 f"day chg={human(r['price_change_percent_atto']):.2f}%"))

check("get_klines(BTCUSDT,1h,5)", lambda: B.get_klines("BTCUSDT", "1h", 5),
      lambda r: (len(r) == 5 and all(c["close_atto"] > 0 for c in r)
                 and r[0]["open_time_ms"] < r[-1]["open_time_ms"],
                 f"{len(r)} candles, last close=${human(r[-1]['close_atto']):,.2f}"))

check("get_twap(BTCUSDT,1h,24)", lambda: B.get_twap("BTCUSDT", "1h", 24),
      lambda r: (int(r) > 0, f"${human(r):,.2f}"))

check("get_symbol_info(BTCUSDT) strict_eq", lambda: B.get_symbol_info("BTCUSDT"),
      lambda r: (r["base_asset"] == "BTC" and r["quote_asset"] == "USDT",
                 f"{r['base_asset']}/{r['quote_asset']} {r['status']}"))

check("get_server_time()", lambda: B.get_server_time(),
      lambda r: (abs(int(r) - int(time.time() * 1000)) < 120_000, f"{int(r)} ms"))

# ---- error path (deterministic [EXTERNAL]) ----
def _bad():
    try:
        B.get_price("NOTAREALPAIRZZZ")
        return "no error raised"
    except UserError as e:  # gl.vm.UserError from the shim
        return e
check("get_price(bad symbol) raises [EXTERNAL]",
      lambda: _bad(),
      lambda r: (isinstance(r, Exception) and str(getattr(r, "message", r)).startswith(fc.ERROR_EXTERNAL),
                 f"raised: {str(getattr(r, 'message', r))[:60]}"))

# ---- composite super-powers ----
check("get_asset_price(BTC,USDT)", lambda: B.get_asset_price("BTC", "USDT"),
      lambda r: (abs(int(r) - btc) * 100 < btc, f"${human(r):,.2f} (~spot)"))

check("get_asset_price(USDT,BTC) inverse-market fallback", lambda: B.get_asset_price("USDT", "BTC"),
      lambda r: (int(r) > 0 and abs(int(r) - (ONE * ONE // btc)) * 100 < (ONE * ONE // btc) + 1,
                 f"{human(r):.10f} BTC (~1/spot)"))

check("convert(BTC->ETH, 1.0)", lambda: B.convert("BTC", "ETH", ONE),
      lambda r: (abs(int(r) - (ONE * btc // eth)) * 100 < (ONE * btc // eth) + 1,
                 f"1 BTC = {human(r):.4f} ETH (ratio {btc/eth:.4f})"))

check("get_median_price(BTC)", lambda: B.get_median_price("BTC", min_sources=1),
      lambda r: (int(r) > 0 and abs(int(r) - btc) * 50 < btc, f"${human(r):,.2f}"))

check("value_basket({0.5 BTC, 3 ETH})",
      lambda: B.value_basket({"BTC": ONE // 2, "ETH": 3 * ONE}),
      lambda r: (abs(int(r) - (btc // 2 + 3 * eth)) * 100 < (btc // 2 + 3 * eth),
                 f"${human(r):,.2f}"))

check("is_price_safe(BTCUSDT)", lambda: B.is_price_safe("BTCUSDT"),
      lambda r: (isinstance(r["safe"], bool) and r["max_divergence_bps"] >= 0,
                 f"safe={r['safe']} divergence={r['max_divergence_bps']}bps"))

check("check_peg(USDC vs USDT)", lambda: B.check_peg("USDC", "USDT"),
      lambda r: (r["deviation_bps"] >= 0, f"pegged={r['pegged']} dev={r['deviation_bps']}bps"))

check("get_volatility(BTCUSDT,1h,24)", lambda: B.get_volatility("BTCUSDT", "1h", 24),
      lambda r: (r["volatility_bps"] >= 0 and r["samples"] >= 1,
                 f"vol={r['volatility_bps']}bps mean={r['mean_return_bps']}bps n={r['samples']}"))

check("get_trend(BTCUSDT,1h,24)", lambda: B.get_trend("BTCUSDT", "1h", 24),
      lambda r: (r["trend"] in ("up", "down", "flat"),
                 f"trend={r['trend']} dev={r['deviation_bps']}bps"))

check("get_liquidity_score(BTCUSDT)", lambda: B.get_liquidity_score("BTCUSDT"),
      lambda r: (0 <= r["score"] <= 100,
                 f"score={r['score']} {dict(r['components'])} spread={r['spread_bps']}bps"))

check("get_execution_price(BTCUSDT, buy, 0.5 BTC)",
      lambda: B.get_execution_price("BTCUSDT", "buy", ONE // 2, 100),
      lambda r: (r["avg_price_atto"] > 0 and r["filled_base_atto"] > 0,
                 f"filled={r['filled']} avg=${human(r['avg_price_atto']):,.2f} levels={r['levels_used']}"))

# price_at: a past, closed candle (3h ago, 1h interval) -> strict_eq, immutable
_past = int(time.time() * 1000) - 3 * 3600 * 1000
check("price_at(BTCUSDT, 3h ago, 1h) strict_eq",
      lambda: B.price_at("BTCUSDT", _past, "1h"),
      lambda r: (int(r) > 0, f"${human(r):,.2f} @ {_past}"))


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------

print()
for status, name, detail in results:
    mark = "[ OK ]" if status == "PASS" else "[FAIL]"
    print(f"{mark} {name:<48} {detail}")
print()
print(f"spot refs: BTC=${btc/ONE:,.2f}  ETH=${eth/ONE:,.2f}")
print(f"validator re-fetches exercised: {REFETCHES['n']}")
print(f"RESULT: {PASS} passed, {FAIL} failed  ({PASS + FAIL} methods)")
sys.exit(1 if FAIL else 0)
