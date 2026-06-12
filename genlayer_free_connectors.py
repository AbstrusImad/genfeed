"""
genlayer_free_connectors
========================

Zero-key, zero-cost external-data connectors for GenLayer Intelligent Contracts.

Full coverage of the **Binance public market-data API** (no API key, no auth)
from inside an Intelligent Contract — one line per reading:

    ``BinanceConnector.get_price("BTCUSDT")``        -> u256 spot price (atto)
    ``BinanceConnector.get_twap("BTCUSDT", "1h", 24)`` -> u256 24h TWAP (atto)
    ``BinanceConnector.get_24h_stats("BTCUSDT")``     -> dict of 24h statistics
    ... see the README for the complete endpoint coverage table.

Design rules enforced throughout (GenLayer consensus requirements):

1.  **Non-deterministic isolation** — every HTTP request happens exclusively
    inside a zero-argument closure that is handed to the GenVM. The
    ``gl.nondet.web.get`` call is written *inline* in each closure, right next
    to the equivalence-principle call that runs it, so auditors and the
    official linter can trace the non-deterministic surface at a glance.
2.  **Per-endpoint consensus strategy** — immutable data (closed candles,
    symbol metadata) uses ``gl.eq_principle.strict_eq`` (byte-exact agreement).
    Live data (spot price, order book, 24h stats) moves between the leader's
    and each validator's fetch, so those methods run a custom validator
    (``gl.vm.run_nondet_unsafe``) in which **every validator independently
    re-fetches the endpoint** and accepts only within an explicit tolerance.
    The validator never rubber-stamps the leader's output.
3.  **Atto-scale integers** — chains must not persist floats. Every decimal
    value is converted to an integer scaled by 10**18 (atto) using pure
    string/integer arithmetic — **no Python float is ever constructed**, so
    results are bit-identical on every validator.
4.  **Semantic error classification** — failures map onto the official
    GenLayer error prefixes so validator agreement is well-defined even on
    failure paths:

    ====================  ======================================================
    Prefix                Meaning
    ====================  ======================================================
    ``[EXPECTED]``        Deterministic business-logic error (bad caller input).
    ``[EXTERNAL]``        Deterministic upstream error (HTTP 4xx, unknown
                          symbol, malformed payload). Validators must observe
                          the *same* message to agree.
    ``[TRANSIENT]``       Temporary upstream error (HTTP 5xx, empty body,
                          connectivity). Validators agree if both hit any
                          transient failure.
    ====================  ======================================================

Compatibility note
------------------
Recent SDK documents describe ``import genlayer as gl`` together with
``from genlayer.types import *``. On the currently pinned public-testnet GenVM
runner (``py-genlayer:1jb45aa8...``) the ``genlayer.types`` module does not
exist and that import raises ``ModuleNotFoundError`` — verified against the
live network. ``from genlayer import *`` exposes the full surface used here
(``gl``, ``u256``, ``Address``, ``TreeMap``, ``DynArray``) and is what
production deployments on the testnet run today.

``gl.nondet.web.get`` is used (rather than ``gl.nondet.web.render``) because it
exposes the HTTP status code, which the official error taxonomy requires:
4xx -> ``[EXTERNAL]`` (deterministic upstream rejection), 5xx -> ``[TRANSIENT]``
(retryable). ``render`` returns only the page text and would make that
classification impossible.

This file is a *library module*: it deliberately carries no runner header.
Deploy it either as part of a multi-file contract package or inlined into a
single deployable file with ``scripts/bundle.py`` (recommended; see README).
"""

from genlayer import *

import json

__all__ = [
    "ERROR_EXPECTED",
    "ERROR_EXTERNAL",
    "ERROR_TRANSIENT",
    "ATTO_DECIMALS",
    "ATTO_SCALE",
    "DEFAULT_TOLERANCE_BPS",
    "to_atto",
    "BinanceConnector",
]

# ---------------------------------------------------------------------------
# Error semantics (official GenLayer prefixes)
# ---------------------------------------------------------------------------

ERROR_EXPECTED = "[EXPECTED]"
ERROR_EXTERNAL = "[EXTERNAL]"
ERROR_TRANSIENT = "[TRANSIENT]"

# ---------------------------------------------------------------------------
# Atto-scale fixed-point arithmetic (string-based, float-free)
# ---------------------------------------------------------------------------

ATTO_DECIMALS = 18
ATTO_SCALE = 10**ATTO_DECIMALS

# Default spread accepted between leader and validator readings in the
# tolerance-based methods: 50 basis points = 0.50 %.
DEFAULT_TOLERANCE_BPS = 50

_MAX_U256 = 2**256 - 1
# Sanity bound for decimal exponents; anything beyond this is a malformed or
# adversarial payload, and unbounded 10**n would waste validator compute.
_MAX_DECIMAL_SHIFT = 96

_JSON_HEADERS = {"Accept": "application/json"}

_BASE = "https://api.binance.com/api/v3"

# Kline intervals accepted by the Binance API.
_VALID_INTERVALS = {
    "1s", "1m", "3m", "5m", "15m", "30m",
    "1h", "2h", "4h", "6h", "8h", "12h",
    "1d", "3d", "1w", "1M",
}


def to_atto(raw: str, *, error_prefix: str = ERROR_EXTERNAL) -> int:
    """Convert a decimal *string* to an atto-scaled integer (value * 10**18).

    The conversion manipulates the digit string directly — splitting on the
    decimal point and shifting by powers of ten — so **no Python float is ever
    created** and the result is bit-identical across validators.

    Supports optional sign and scientific notation. Fractional digits beyond
    18 decimals are truncated toward zero.

    Args:
        raw: Decimal literal, e.g. ``"43250.12"``, ``"-3.5"``, ``"8.2e-08"``.
        error_prefix: GenLayer error class used when parsing fails. Defaults to
            ``[EXTERNAL]`` because this helper normally parses upstream payloads.

    Returns:
        The signed atto-scaled integer.

    Raises:
        gl.vm.UserError: with ``error_prefix`` when ``raw`` is not a valid
            decimal literal or the magnitude is out of range.
    """
    text = str(raw).strip().lower()
    if not text:
        raise gl.vm.UserError(f"{error_prefix} Empty numeric value")

    negative = text.startswith("-")
    if text[0] in "+-":
        text = text[1:]

    # Split scientific notation: "<mantissa>e<exponent>"
    if "e" in text:
        mantissa, _, exp_text = text.partition("e")
        try:
            exponent = int(exp_text, 10)
        except ValueError:
            raise gl.vm.UserError(f"{error_prefix} Invalid exponent in numeric value: '{raw}'")
    else:
        mantissa, exponent = text, 0

    int_part, _, frac_part = mantissa.partition(".")
    digits = (int_part + frac_part) or "0"
    if not digits.isdigit():
        raise gl.vm.UserError(f"{error_prefix} Invalid decimal literal: '{raw}'")

    # The integer `digits` carries an implicit factor of 10**(exponent - len(frac)).
    shift = exponent - len(frac_part) + ATTO_DECIMALS
    if shift > _MAX_DECIMAL_SHIFT or shift < -_MAX_DECIMAL_SHIFT:
        raise gl.vm.UserError(f"{error_prefix} Numeric value out of range: '{raw}'")

    magnitude = int(digits, 10)
    if shift >= 0:
        atto = magnitude * 10**shift
    else:
        atto = magnitude // 10**(-shift)  # truncate extra precision toward zero

    if atto > _MAX_U256:
        raise gl.vm.UserError(f"{error_prefix} Numeric value exceeds u256 range: '{raw}'")

    return -atto if negative else atto


# ---------------------------------------------------------------------------
# Deterministic input validation (runs BEFORE any non-deterministic block)
# ---------------------------------------------------------------------------


def _clean_symbol(symbol: str) -> str:
    """Validate and normalize a Binance market symbol (e.g. ``BTCUSDT``).

    Restricting the charset to alphanumerics also makes the value safe to
    interpolate into the request URL (no query-string injection).
    """
    cleaned = str(symbol or "").strip().upper()
    if not cleaned or len(cleaned) > 20 or not cleaned.isalnum():
        raise gl.vm.UserError(f"{ERROR_EXPECTED} Invalid market symbol: '{symbol}'")
    return cleaned


def _clean_interval(interval: str) -> str:
    """Validate a kline interval against the Binance whitelist."""
    cleaned = str(interval or "").strip()
    if cleaned not in _VALID_INTERVALS:
        raise gl.vm.UserError(
            f"{ERROR_EXPECTED} Invalid kline interval: '{interval}' "
            f"(expected one of {sorted(_VALID_INTERVALS)})"
        )
    return cleaned


def _clean_window(window_size: str) -> str:
    """Validate a rolling-window size: 1-59m, 1-23h or 1-7d (Binance rules)."""
    cleaned = str(window_size or "").strip()
    unit = cleaned[-1:] if cleaned else ""
    amount = cleaned[:-1]
    if unit in ("m", "h", "d") and amount.isdigit():
        value = int(amount, 10)
        if (
            (unit == "m" and 1 <= value <= 59)
            or (unit == "h" and 1 <= value <= 23)
            or (unit == "d" and 1 <= value <= 7)
        ):
            return cleaned
    raise gl.vm.UserError(f"{ERROR_EXPECTED} Invalid window size: '{window_size}'")


def _check_range(value: int, lo: int, hi: int, what: str) -> int:
    """Validate an integer caller parameter within [lo, hi]."""
    if not isinstance(value, int) or value < lo or value > hi:
        raise gl.vm.UserError(f"{ERROR_EXPECTED} {what} must be an integer in [{lo}, {hi}]")
    return value


def _check_tolerance(tolerance_bps: int) -> int:
    """Validate a basis-point tolerance parameter (0 = byte-exact)."""
    return _check_range(tolerance_bps, 0, 10_000, "tolerance_bps")


# ---------------------------------------------------------------------------
# Deterministic response parsing (shared by the closures; contains no
# non-deterministic calls, so it is safe to factor out of the closures)
# ---------------------------------------------------------------------------


def _expect_json(response, url: str):
    """Classify HTTP status and decode the JSON body (object or array).

    Pure deterministic post-processing of an already-performed web request —
    the request itself must live inside the non-deterministic closure.

    ``parse_float=str`` keeps decimal literals as exact strings so they reach
    :func:`to_atto` without ever becoming Python floats.
    """
    status = int(response.status)
    if 400 <= status < 500:
        # Unknown symbols / bad params answer 4xx -> deterministic rejection.
        raise gl.vm.UserError(f"{ERROR_EXTERNAL} HTTP {status} from {url}")
    if status >= 500:
        raise gl.vm.UserError(f"{ERROR_TRANSIENT} HTTP {status} from {url}")

    body = response.body
    if not body:
        raise gl.vm.UserError(f"{ERROR_TRANSIENT} Empty response body from {url}")

    try:
        return json.loads(body.decode("utf-8"), parse_float=str)
    except (UnicodeDecodeError, ValueError):
        raise gl.vm.UserError(f"{ERROR_TRANSIENT} Non-JSON response from {url}")


def _expect_object(response, url: str) -> dict:
    """Like :func:`_expect_json` but requires a JSON object."""
    payload = _expect_json(response, url)
    if not isinstance(payload, dict):
        raise gl.vm.UserError(f"{ERROR_EXTERNAL} Expected JSON object from {url}")
    return payload


def _expect_array(response, url: str) -> list:
    """Like :func:`_expect_json` but requires a JSON array."""
    payload = _expect_json(response, url)
    if not isinstance(payload, list):
        raise gl.vm.UserError(f"{ERROR_EXTERNAL} Expected JSON array from {url}")
    return payload


def _field(payload: dict, key: str, url: str):
    """Extract a required field from an upstream payload."""
    value = payload.get(key)
    if value is None:
        raise gl.vm.UserError(f"{ERROR_EXTERNAL} Missing field '{key}' in response from {url}")
    return value


def _field_atto(payload: dict, key: str, url: str) -> int:
    """Extract a required decimal field and convert it to atto scale."""
    return to_atto(str(_field(payload, key, url)))


def _field_int(payload: dict, key: str, url: str) -> int:
    """Extract a required integer field (timestamps, trade counts, ids)."""
    raw = _field(payload, key, url)
    try:
        return int(str(raw).strip(), 10)
    except ValueError:
        raise gl.vm.UserError(f"{ERROR_EXTERNAL} Non-integer field '{key}' in response from {url}")


def _parse_stats_payload(payload: dict, url: str) -> dict:
    """Normalize a 24h / rolling-window ticker payload to atto-scaled fields.

    Signed fields (price change, percent change) keep their sign; all decimal
    fields are atto-scaled; window timestamps and trade count are plain ints.
    """
    return {
        "last_atto": _field_atto(payload, "lastPrice", url),
        "open_atto": _field_atto(payload, "openPrice", url),
        "high_atto": _field_atto(payload, "highPrice", url),
        "low_atto": _field_atto(payload, "lowPrice", url),
        "weighted_avg_atto": _field_atto(payload, "weightedAvgPrice", url),
        "price_change_atto": _field_atto(payload, "priceChange", url),
        "price_change_percent_atto": _field_atto(payload, "priceChangePercent", url),
        "base_volume_atto": _field_atto(payload, "volume", url),
        "quote_volume_atto": _field_atto(payload, "quoteVolume", url),
        "open_time_ms": _field_int(payload, "openTime", url),
        "close_time_ms": _field_int(payload, "closeTime", url),
        "trade_count": _field_int(payload, "count", url),
    }


def _parse_klines_payload(rows: list, url: str) -> list:
    """Normalize raw kline rows ([openTime, o, h, l, c, vol, closeTime, ...])."""
    candles = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 9:
            raise gl.vm.UserError(f"{ERROR_EXTERNAL} Malformed kline row from {url}")
        candles.append(
            {
                "open_time_ms": int(str(row[0]), 10),
                "open_atto": to_atto(str(row[1])),
                "high_atto": to_atto(str(row[2])),
                "low_atto": to_atto(str(row[3])),
                "close_atto": to_atto(str(row[4])),
                "base_volume_atto": to_atto(str(row[5])),
                "close_time_ms": int(str(row[6]), 10),
                "quote_volume_atto": to_atto(str(row[7])),
                "trade_count": int(str(row[8]), 10),
            }
        )
    return candles


def _plain(value):
    """Recursively normalize calldata containers (Map -> dict, etc.)."""
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if hasattr(value, "items") and not isinstance(value, (str, bytes)):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


# ---------------------------------------------------------------------------
# Consensus plumbing
# ---------------------------------------------------------------------------


def _within_bps(a: int, b: int, tolerance_bps: int) -> bool:
    """Integer-only check: |a - b| <= tolerance_bps/10000 of max(|a|, |b|).

    Sign-aware: values of opposite sign never agree (a price change of +0.1%
    and -0.1% are different market facts, however close numerically).
    """
    if a == b:
        return True
    if (a < 0) != (b < 0):
        return False
    a_abs, b_abs = abs(a), abs(b)
    return abs(a_abs - b_abs) * 10_000 <= tolerance_bps * max(a_abs, b_abs)


def _agree_on_error(leaders_res, leader_fn) -> bool:
    """Canonical validator behaviour when the leader's closure errored.

    - ``[EXTERNAL]`` errors are deterministic: the validator must reproduce the
      *exact same* message to agree.
    - ``[TRANSIENT]`` errors: validators agree if both sides hit any transient
      failure (messages may differ).
    - Anything else (validator succeeded, unknown error class): disagree, which
      forces leader rotation instead of locking bad state on-chain.
    """
    leader_msg = getattr(leaders_res, "message", "") or ""
    try:
        leader_fn()
        return False  # Leader failed but the validator succeeded -> disagree.
    except gl.vm.UserError as exc:
        validator_msg = getattr(exc, "message", None) or str(exc)
        if validator_msg.startswith(ERROR_EXTERNAL):
            return validator_msg == leader_msg
        if validator_msg.startswith(ERROR_TRANSIENT) and leader_msg.startswith(
            ERROR_TRANSIENT
        ):
            return True
        return False
    except Exception:
        return False


def _make_validator(fetch, agree):
    """Build the canonical re-fetching validator for ``run_nondet_unsafe``.

    The validator always produces **independent evidence**: it re-runs the
    same fetch closure and applies ``agree(leader_value, validator_value)``.
    It never accepts the leader's output on format alone.
    """

    def validator_fn(leaders_res) -> bool:
        if not isinstance(leaders_res, gl.vm.Return):
            return _agree_on_error(leaders_res, fetch)
        try:
            mine = fetch()
        except Exception:
            return False  # Leader succeeded, validator could not verify.
        return agree(_plain(leaders_res.calldata), _plain(mine))

    return validator_fn


def _stats_agree(theirs: dict, mine: dict, tolerance_bps: int) -> bool:
    """Field-wise agreement for 24h / rolling ticker statistics."""
    if not isinstance(theirs, dict) or not isinstance(mine, dict):
        return False
    atto_fields = (
        "last_atto", "open_atto", "high_atto", "low_atto", "weighted_avg_atto",
        "price_change_atto", "price_change_percent_atto",
        "base_volume_atto", "quote_volume_atto",
    )
    for key in atto_fields:
        if key not in theirs or key not in mine:
            return False
        if not _within_bps(int(theirs[key]), int(mine[key]), tolerance_bps):
            return False
    if not _within_bps(int(theirs.get("trade_count", -1)), int(mine.get("trade_count", -2)), tolerance_bps):
        return False
    # The stats window slides continuously; allow a 5-minute skew on bounds.
    for key in ("open_time_ms", "close_time_ms"):
        if abs(int(theirs.get(key, 0)) - int(mine.get(key, 0))) > 300_000:
            return False
    return True


def _klines_agree(theirs: list, mine: list) -> bool:
    """Agreement rule for closed-candle lists fetched without a pinned endTime.

    Closed candles are immutable, so any candle present in both fetches must
    match **byte-exactly**. Because the window slides between the leader's and
    the validator's requests, each side may see at most one candle the other
    does not (one falls off the left edge, one closes on the right edge).
    """
    if not isinstance(theirs, list) or not isinstance(mine, list) or not theirs:
        return False
    by_open_theirs = {int(c["open_time_ms"]): c for c in theirs if isinstance(c, dict)}
    by_open_mine = {int(c["open_time_ms"]): c for c in mine if isinstance(c, dict)}
    shared = by_open_theirs.keys() & by_open_mine.keys()
    if not shared:
        return False
    if len(by_open_theirs.keys() - shared) > 1 or len(by_open_mine.keys() - shared) > 1:
        return False
    return all(by_open_theirs[k] == by_open_mine[k] for k in shared)


# ---------------------------------------------------------------------------
# BinanceConnector — full public market-data API coverage
# ---------------------------------------------------------------------------


class BinanceConnector:
    """Complete, consensus-aware client for the Binance public REST API.

    Every method validates inputs deterministically, performs the web request
    inside a zero-argument non-deterministic closure, normalizes decimals to
    atto-scale integers, and reaches validator agreement with the strategy
    that matches the data's volatility:

    =============================  =========================================
    Method                         Consensus strategy
    =============================  =========================================
    ``get_price``                  ``strict_eq`` (spec-exact)
    ``get_price_with_tolerance``   re-fetch + price within bps
    ``get_avg_price``              re-fetch + price within bps
    ``get_book_ticker``            re-fetch + bid/ask within bps
    ``get_order_book_summary``     re-fetch + prices/volumes within bps
    ``get_24h_stats``              re-fetch + field-wise bps agreement
    ``get_rolling_stats``          re-fetch + field-wise bps agreement
    ``get_klines``                 ``strict_eq`` when ``end_time_ms`` pinned;
                                   immutable-candle overlap rule otherwise
    ``get_twap``                   re-fetch + TWAP within bps (default 10)
    ``get_symbol_info``            ``strict_eq`` (stable metadata)
    ``get_server_time``            re-fetch + skew within N seconds
    =============================  =========================================

    Not exposed (and why): ``/ping`` carries no data; ``/trades`` and
    ``/aggTrades`` are per-trade streams that cannot reach meaningful
    validator agreement (use ``get_klines`` — candles *are* aggregated
    trades); ``/historicalTrades`` requires an API key; the full
    ``/exchangeInfo`` dump is a multi-megabyte payload unfit for validator
    compute budgets (the per-symbol form **is** exposed via
    ``get_symbol_info``).

    Operational note: Binance answers HTTP 451 from a small set of restricted
    regions. A region-blocked validator simply disagrees and consensus
    rotates the leader; it cannot corrupt state.
    """

    BASE_URL = _BASE

    # ------------------------------------------------------------------
    # Spot price — /api/v3/ticker/price
    # ------------------------------------------------------------------

    @staticmethod
    def get_price(symbol: str) -> u256:
        """Current spot price of ``symbol`` as an atto-scaled ``u256``.

        Byte-exact consensus (``strict_eq``): ideal for quiet markets and for
        the strictest reproducibility requirements. On fast markets prefer
        :meth:`get_price_with_tolerance`.

        Args:
            symbol: Binance market symbol, e.g. ``"BTCUSDT"`` (case-insensitive).

        Returns:
            Price multiplied by 10**18
            (e.g. 43250.12 USDT -> ``43250120000000000000000``).
        """
        cleaned = _clean_symbol(symbol)
        url = f"{_BASE}/ticker/price?symbol={cleaned}"

        def fetch() -> int:
            response = gl.nondet.web.get(url, headers=_JSON_HEADERS)
            payload = _expect_object(response, url)
            return _field_atto(payload, "price", url)

        atto = gl.eq_principle.strict_eq(fetch)
        return u256(atto)

    @staticmethod
    def get_price_with_tolerance(
        symbol: str, tolerance_bps: int = DEFAULT_TOLERANCE_BPS
    ) -> u256:
        """Like :meth:`get_price`, but resilient to market micro-movements.

        The leader proposes a reading; each validator independently re-fetches
        the same endpoint and accepts iff both readings agree within
        ``tolerance_bps`` basis points (default 0.50%). The leader's value is
        what gets returned (and therefore stored).
        """
        _check_tolerance(tolerance_bps)
        cleaned = _clean_symbol(symbol)
        url = f"{_BASE}/ticker/price?symbol={cleaned}"

        def fetch() -> int:
            response = gl.nondet.web.get(url, headers=_JSON_HEADERS)
            payload = _expect_object(response, url)
            return _field_atto(payload, "price", url)

        def agree(theirs, mine) -> bool:
            return isinstance(theirs, int) and theirs >= 0 and _within_bps(theirs, mine, tolerance_bps)

        atto = gl.vm.run_nondet_unsafe(fetch, _make_validator(fetch, agree))
        return u256(atto)

    # ------------------------------------------------------------------
    # Current average price (5-minute window) — /api/v3/avgPrice
    # ------------------------------------------------------------------

    @staticmethod
    def get_avg_price(
        symbol: str, tolerance_bps: int = DEFAULT_TOLERANCE_BPS
    ) -> u256:
        """Binance's rolling 5-minute average price, atto-scaled.

        Smoother than the instantaneous spot price — a better fit for
        settlements. Validators re-fetch and agree within ``tolerance_bps``.
        """
        _check_tolerance(tolerance_bps)
        cleaned = _clean_symbol(symbol)
        url = f"{_BASE}/avgPrice?symbol={cleaned}"

        def fetch() -> int:
            response = gl.nondet.web.get(url, headers=_JSON_HEADERS)
            payload = _expect_object(response, url)
            return _field_atto(payload, "price", url)

        def agree(theirs, mine) -> bool:
            return isinstance(theirs, int) and theirs >= 0 and _within_bps(theirs, mine, tolerance_bps)

        atto = gl.vm.run_nondet_unsafe(fetch, _make_validator(fetch, agree))
        return u256(atto)

    # ------------------------------------------------------------------
    # Best bid/ask — /api/v3/ticker/bookTicker
    # ------------------------------------------------------------------

    @staticmethod
    def get_book_ticker(
        symbol: str, tolerance_bps: int = DEFAULT_TOLERANCE_BPS
    ) -> dict:
        """Top of the order book: best bid, best ask, mid price and spread.

        Returns ``{"bid_atto", "ask_atto", "mid_atto", "spread_bps"}`` (ints).
        ``mid`` and ``spread`` are derived deterministically from bid/ask.
        Validators re-fetch and require bid and ask within ``tolerance_bps``.
        """
        _check_tolerance(tolerance_bps)
        cleaned = _clean_symbol(symbol)
        url = f"{_BASE}/ticker/bookTicker?symbol={cleaned}"

        def fetch() -> dict:
            response = gl.nondet.web.get(url, headers=_JSON_HEADERS)
            payload = _expect_object(response, url)
            bid = _field_atto(payload, "bidPrice", url)
            ask = _field_atto(payload, "askPrice", url)
            if bid <= 0 or ask <= 0 or ask < bid:
                raise gl.vm.UserError(f"{ERROR_EXTERNAL} Inconsistent book ticker from {url}")
            mid = (bid + ask) // 2
            spread_bps = ((ask - bid) * 10_000) // mid if mid else 0
            return {"bid_atto": bid, "ask_atto": ask, "mid_atto": mid, "spread_bps": spread_bps}

        def agree(theirs, mine) -> bool:
            if not isinstance(theirs, dict) or not isinstance(mine, dict):
                return False
            return _within_bps(
                int(theirs.get("bid_atto", -1)), int(mine.get("bid_atto", -2)), tolerance_bps
            ) and _within_bps(
                int(theirs.get("ask_atto", -1)), int(mine.get("ask_atto", -2)), tolerance_bps
            )

        return gl.vm.run_nondet_unsafe(fetch, _make_validator(fetch, agree))

    # ------------------------------------------------------------------
    # Order-book depth summary — /api/v3/depth
    # ------------------------------------------------------------------

    @staticmethod
    def get_order_book_summary(
        symbol: str,
        levels: int = 100,
        price_tolerance_bps: int = DEFAULT_TOLERANCE_BPS,
        volume_tolerance_bps: int = 2_000,
    ) -> dict:
        """Aggregated liquidity over the top ``levels`` of the order book.

        Returns ``{"best_bid_atto", "best_ask_atto", "bid_volume_atto",
        "ask_volume_atto", "levels"}``. Individual book levels churn far too
        fast for consensus, so the connector aggregates: best prices agree
        within ``price_tolerance_bps``; summed volumes within
        ``volume_tolerance_bps`` (default 20% — top-of-book liquidity swings
        hard between any two fetches).
        """
        _check_tolerance(price_tolerance_bps)
        _check_tolerance(volume_tolerance_bps)
        _check_range(levels, 1, 5_000, "levels")
        cleaned = _clean_symbol(symbol)
        url = f"{_BASE}/depth?symbol={cleaned}&limit={levels}"

        def fetch() -> dict:
            response = gl.nondet.web.get(url, headers=_JSON_HEADERS)
            payload = _expect_object(response, url)
            bids = _field(payload, "bids", url)
            asks = _field(payload, "asks", url)
            if not isinstance(bids, list) or not isinstance(asks, list) or not bids or not asks:
                raise gl.vm.UserError(f"{ERROR_EXTERNAL} Empty order book from {url}")
            return {
                "best_bid_atto": to_atto(str(bids[0][0])),
                "best_ask_atto": to_atto(str(asks[0][0])),
                "bid_volume_atto": sum(to_atto(str(level[1])) for level in bids),
                "ask_volume_atto": sum(to_atto(str(level[1])) for level in asks),
                "levels": len(bids),
            }

        def agree(theirs, mine) -> bool:
            if not isinstance(theirs, dict) or not isinstance(mine, dict):
                return False
            for key, bps in (
                ("best_bid_atto", price_tolerance_bps),
                ("best_ask_atto", price_tolerance_bps),
                ("bid_volume_atto", volume_tolerance_bps),
                ("ask_volume_atto", volume_tolerance_bps),
            ):
                if not _within_bps(int(theirs.get(key, -1)), int(mine.get(key, -2)), bps):
                    return False
            return True

        return gl.vm.run_nondet_unsafe(fetch, _make_validator(fetch, agree))

    # ------------------------------------------------------------------
    # 24h statistics — /api/v3/ticker/24hr
    # ------------------------------------------------------------------

    @staticmethod
    def get_24h_stats(symbol: str, tolerance_bps: int = 100) -> dict:
        """Full 24-hour rolling statistics for ``symbol``.

        Returns atto-scaled ``last/open/high/low/weighted_avg``, **signed**
        ``price_change`` and ``price_change_percent``, base/quote volumes,
        window timestamps and the trade count. The 24h window slides
        continuously, so the default tolerance is 100 bps (1%); validators
        re-fetch and compare field-wise (sign-aware).
        """
        _check_tolerance(tolerance_bps)
        cleaned = _clean_symbol(symbol)
        url = f"{_BASE}/ticker/24hr?symbol={cleaned}"

        def fetch() -> dict:
            response = gl.nondet.web.get(url, headers=_JSON_HEADERS)
            return _parse_stats_payload(_expect_object(response, url), url)

        def agree(theirs, mine) -> bool:
            return _stats_agree(theirs, mine, tolerance_bps)

        return gl.vm.run_nondet_unsafe(fetch, _make_validator(fetch, agree))

    # ------------------------------------------------------------------
    # Rolling-window statistics — /api/v3/ticker
    # ------------------------------------------------------------------

    @staticmethod
    def get_rolling_stats(
        symbol: str, window_size: str = "1d", tolerance_bps: int = 100
    ) -> dict:
        """Same shape as :meth:`get_24h_stats`, over a custom rolling window.

        ``window_size`` accepts ``1m``-``59m``, ``1h``-``23h`` or ``1d``-``7d``
        (Binance rules).
        """
        _check_tolerance(tolerance_bps)
        cleaned = _clean_symbol(symbol)
        window = _clean_window(window_size)
        url = f"{_BASE}/ticker?symbol={cleaned}&windowSize={window}"

        def fetch() -> dict:
            response = gl.nondet.web.get(url, headers=_JSON_HEADERS)
            return _parse_stats_payload(_expect_object(response, url), url)

        def agree(theirs, mine) -> bool:
            return _stats_agree(theirs, mine, tolerance_bps)

        return gl.vm.run_nondet_unsafe(fetch, _make_validator(fetch, agree))

    # ------------------------------------------------------------------
    # Candlesticks — /api/v3/klines
    # ------------------------------------------------------------------

    @staticmethod
    def get_klines(
        symbol: str,
        interval: str = "1h",
        limit: int = 24,
        end_time_ms: int = 0,
    ) -> list:
        """Closed (immutable) candlesticks, oldest first.

        Each candle is ``{"open_time_ms", "open_atto", "high_atto",
        "low_atto", "close_atto", "base_volume_atto", "close_time_ms",
        "quote_volume_atto", "trade_count"}``. The most recent (potentially
        still-open) candle is always dropped, so only finalized data reaches
        the chain.

        Consensus: with ``end_time_ms`` pinned to a past timestamp the window
        is fully deterministic and ``strict_eq`` applies. Without it, the
        window slides between fetches, so validators apply the
        immutable-candle overlap rule: shared candles must match byte-exactly
        and each side may differ by at most one boundary candle.

        Args:
            symbol: Market symbol, e.g. ``"BTCUSDT"``.
            interval: One of the Binance kline intervals (``"1m"`` ... ``"1M"``).
            limit: Number of closed candles to return (1-500).
            end_time_ms: Optional fixed window end (epoch milliseconds). Pin it
                (e.g. from :meth:`get_server_time`) for strict determinism.
        """
        cleaned = _clean_symbol(symbol)
        cleaned_interval = _clean_interval(interval)
        _check_range(limit, 1, 500, "limit")
        _check_range(end_time_ms, 0, 4_102_444_800_000, "end_time_ms")  # <= year 2100

        url = (
            f"{_BASE}/klines?symbol={cleaned}&interval={cleaned_interval}"
            f"&limit={limit + 1}"
        )
        if end_time_ms > 0:
            url += f"&endTime={end_time_ms}"

        def fetch() -> list:
            response = gl.nondet.web.get(url, headers=_JSON_HEADERS)
            rows = _expect_array(response, url)
            if not rows:
                raise gl.vm.UserError(f"{ERROR_EXTERNAL} No klines returned from {url}")
            candles = _parse_klines_payload(rows, url)
            # Drop the most recent candle: it may still be open (mutable).
            closed = candles[:-1] if len(candles) > 1 else candles
            return closed[-limit:]

        if end_time_ms > 0:
            return gl.eq_principle.strict_eq(fetch)
        return gl.vm.run_nondet_unsafe(fetch, _make_validator(fetch, _klines_agree))

    # ------------------------------------------------------------------
    # TWAP — derived from closed candles (manipulation-resistant price)
    # ------------------------------------------------------------------

    @staticmethod
    def get_twap(
        symbol: str,
        interval: str = "1h",
        periods: int = 24,
        tolerance_bps: int = 10,
    ) -> u256:
        """Time-weighted average price over the last ``periods`` closed candles.

        The classic oracle primitive: averaging closed candles makes the
        reading far harder to manipulate than any instantaneous price. Since
        closed candles are immutable, leader and validator averages differ
        only by the sliding boundary candle — hence the tight default
        tolerance of 10 bps (0.10%).
        """
        _check_tolerance(tolerance_bps)
        cleaned = _clean_symbol(symbol)
        cleaned_interval = _clean_interval(interval)
        _check_range(periods, 1, 500, "periods")
        url = (
            f"{_BASE}/klines?symbol={cleaned}&interval={cleaned_interval}"
            f"&limit={periods + 1}"
        )

        def fetch() -> int:
            response = gl.nondet.web.get(url, headers=_JSON_HEADERS)
            rows = _expect_array(response, url)
            if not rows:
                raise gl.vm.UserError(f"{ERROR_EXTERNAL} No klines returned from {url}")
            candles = _parse_klines_payload(rows, url)
            closed = candles[:-1] if len(candles) > 1 else candles
            window = closed[-periods:]
            total = sum(int(c["close_atto"]) for c in window)
            return total // len(window)

        def agree(theirs, mine) -> bool:
            return isinstance(theirs, int) and theirs >= 0 and _within_bps(theirs, mine, tolerance_bps)

        atto = gl.vm.run_nondet_unsafe(fetch, _make_validator(fetch, agree))
        return u256(atto)

    # ------------------------------------------------------------------
    # Symbol metadata — /api/v3/exchangeInfo?symbol=
    # ------------------------------------------------------------------

    @staticmethod
    def get_symbol_info(symbol: str) -> dict:
        """Stable trading metadata for one symbol (``strict_eq``).

        Returns ``{"symbol", "status", "base_asset", "quote_asset",
        "is_spot_trading_allowed"}``. Metadata changes only on listing events,
        so byte-exact consensus is appropriate.
        """
        cleaned = _clean_symbol(symbol)
        url = f"{_BASE}/exchangeInfo?symbol={cleaned}"

        def fetch() -> dict:
            response = gl.nondet.web.get(url, headers=_JSON_HEADERS)
            payload = _expect_object(response, url)
            symbols = _field(payload, "symbols", url)
            if not isinstance(symbols, list) or not symbols or not isinstance(symbols[0], dict):
                raise gl.vm.UserError(f"{ERROR_EXTERNAL} Unknown symbol '{cleaned}'")
            info = symbols[0]
            return {
                "symbol": str(info.get("symbol", cleaned)),
                "status": str(info.get("status", "")),
                "base_asset": str(info.get("baseAsset", "")),
                "quote_asset": str(info.get("quoteAsset", "")),
                "is_spot_trading_allowed": bool(info.get("isSpotTradingAllowed", False)),
            }

        return gl.eq_principle.strict_eq(fetch)

    # ------------------------------------------------------------------
    # Server time — /api/v3/time (a free external clock for contracts)
    # ------------------------------------------------------------------

    @staticmethod
    def get_server_time(tolerance_seconds: int = 10) -> u256:
        """Binance server time as epoch **milliseconds** (``u256``).

        Useful as a decentralized clock source (e.g. to pin
        :meth:`get_klines` windows). Validators re-fetch and agree when their
        own reading is within ``tolerance_seconds`` of the leader's.
        """
        _check_range(tolerance_seconds, 1, 300, "tolerance_seconds")
        url = f"{_BASE}/time"

        def fetch() -> int:
            response = gl.nondet.web.get(url, headers=_JSON_HEADERS)
            payload = _expect_object(response, url)
            return _field_int(payload, "serverTime", url)

        def agree(theirs, mine) -> bool:
            return (
                isinstance(theirs, int)
                and theirs > 0
                and abs(theirs - mine) <= tolerance_seconds * 1_000
            )

        ms = gl.vm.run_nondet_unsafe(fetch, _make_validator(fetch, agree))
        return u256(ms)
