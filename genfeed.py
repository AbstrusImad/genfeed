"""
GenFeed
=======

Zero-key, zero-cost external market-data for GenLayer Intelligent Contracts.

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

# Heuristic thresholds for :meth:`BinanceConnector.get_liquidity_score`. These
# are tunable scoring conventions, not exchange-defined values; they are named
# constants (rather than magic numbers) so integrators can reason about and
# re-derive the 0-100 score deterministically.
_LIQ_SPREAD_MAX_BPS = 200        # spread at/above which the spread score is 0
_LIQ_DEPTH_FULL_QUOTE = 1_000_000      # top-book quote depth scoring full marks
_LIQ_VOLUME_FULL_QUOTE = 100_000_000   # 24h quote volume scoring full marks

# Default stablecoin markets used by :meth:`BinanceConnector.get_median_price`
# to build a manipulation-resistant USD price for an asset.
_DEFAULT_MEDIAN_QUOTES = ("USDT", "USDC", "FDUSD")


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


def _clean_asset(asset: str) -> str:
    """Validate and normalize a single asset code (e.g. ``BTC``, ``USDT``).

    Like :func:`_clean_symbol` but for one leg of a pair; the alphanumeric
    restriction keeps it safe to concatenate into a market symbol and a URL.
    """
    cleaned = str(asset or "").strip().upper()
    if not cleaned or len(cleaned) > 12 or not cleaned.isalnum():
        raise gl.vm.UserError(f"{ERROR_EXPECTED} Invalid asset code: '{asset}'")
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


def _clean_timezone(time_zone: str) -> str:
    """Validate a Binance ``timeZone`` offset and URL-encode it.

    Accepts whole hours (``"0"``, ``"-5"``, ``"8"``) or ``"HH:MM"``
    (``"5:45"``), in Binance's supported ``-12:00``..``+14:00`` range. The
    ``:`` is percent-encoded so the value is safe to interpolate into the URL.
    """
    raw = str(time_zone if time_zone is not None else "0").strip()
    sign = ""
    body = raw
    if body[:1] in "+-":
        sign = "-" if body[0] == "-" else ""
        body = body[1:]
    hours, _, minutes = body.partition(":")
    if not hours.isdigit() or int(hours) > 14:
        raise gl.vm.UserError(f"{ERROR_EXPECTED} Invalid timeZone: '{time_zone}'")
    if minutes and (len(minutes) != 2 or not minutes.isdigit() or int(minutes) >= 60):
        raise gl.vm.UserError(f"{ERROR_EXPECTED} Invalid timeZone minutes: '{time_zone}'")
    cleaned = f"{sign}{int(hours)}"
    if minutes:
        cleaned += f"%3A{minutes}"
    return cleaned


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


def _isqrt(n: int) -> int:
    """Deterministic integer square root (floor) via integer Newton's method.

    Implemented by hand rather than via ``math.isqrt`` so the library carries
    no math-module dependency and the result is provably float-free and
    bit-identical across validators (used by :meth:`get_volatility`).
    """
    if n < 0:
        raise gl.vm.UserError(f"{ERROR_EXPECTED} isqrt of negative value")
    if n == 0:
        return 0
    x = n
    y = (x + 1) // 2
    while y < x:
        x = y
        y = (x + n // x) // 2
    return x


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
    ``get_trading_day_stats``      re-fetch + field-wise bps agreement
    ``get_klines``                 ``strict_eq`` when ``end_time_ms`` pinned;
                                   immutable-candle overlap rule otherwise
    ``get_twap``                   re-fetch + TWAP within bps (default 10)
    ``get_symbol_info``            ``strict_eq`` (stable metadata)
    ``get_server_time``            re-fetch + skew within N seconds
    ``price_at``                   ``strict_eq`` (pinned past candle)
    =============================  =========================================

    Composite "super-power" methods — ``get_asset_price``, ``convert``,
    ``get_median_price``, ``value_basket``, ``is_price_safe``, ``check_peg``,
    ``get_volatility``, ``get_trend``, ``get_liquidity_score`` and
    ``get_execution_price`` — build higher-level financial primitives on top
    of the raw endpoints. They issue **no direct non-deterministic calls**:
    each composes already-validated readings (every ``get_*`` sub-call runs
    its own equivalence-principle block, returning the leader-agreed value to
    all nodes) and then applies pure integer arithmetic, so every validator
    reproduces the derived result bit-for-bit. See the README for the full
    composite table.

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
        levels: int = 500,
        price_tolerance_bps: int = DEFAULT_TOLERANCE_BPS,
        volume_tolerance_bps: int = 5_000,
    ) -> dict:
        """Aggregated liquidity over the top ``levels`` of the order book.

        Returns ``{"best_bid_atto", "best_ask_atto", "bid_volume_atto",
        "ask_volume_atto", "levels"}``.

        Consensus note (measured against live Binance): best **prices** are
        stable and agree within ``price_tolerance_bps``. Summed **volume** is
        noise-dominated at shallow depth — the top ~20-100 levels can swing
        60-95% between two fetches as small orders churn — but stabilises with
        depth, because large resting orders dominate the sum (the top 500
        levels swing only a few percent even over multi-second gaps). The
        default therefore reads ``levels=500`` and accepts summed volume
        within ``volume_tolerance_bps`` (50%), treating depth as a coarse
        liquidity *magnitude*, not a precise figure. Request a shallow
        ``levels`` only if you also raise ``volume_tolerance_bps``; for a
        stable liquidity number prefer 24h quote volume (``get_24h_stats``).
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

    # ------------------------------------------------------------------
    # Trading-day statistics — /api/v3/ticker/tradingDay
    # ------------------------------------------------------------------

    @staticmethod
    def get_trading_day_stats(
        symbol: str, time_zone: str = "0", tolerance_bps: int = 100
    ) -> dict:
        """Statistics for the current **calendar trading day**.

        Same shape as :meth:`get_24h_stats`, but the window is the trading day
        in ``time_zone`` (``"0"`` UTC, ``"-5"``, ``"8"``, ``"5:45"``) rather
        than a rolling 24h. The day keeps accumulating until it closes, so it
        is treated as live data: validators re-fetch and agree field-wise
        within ``tolerance_bps`` (default 100 bps).
        """
        _check_tolerance(tolerance_bps)
        cleaned = _clean_symbol(symbol)
        tz = _clean_timezone(time_zone)
        url = f"{_BASE}/ticker/tradingDay?symbol={cleaned}&timeZone={tz}"

        def fetch() -> dict:
            response = gl.nondet.web.get(url, headers=_JSON_HEADERS)
            return _parse_stats_payload(_expect_object(response, url), url)

        def agree(theirs, mine) -> bool:
            return _stats_agree(theirs, mine, tolerance_bps)

        return gl.vm.run_nondet_unsafe(fetch, _make_validator(fetch, agree))

    # ==================================================================
    # Composite / derived "super-power" methods
    #
    # Each method below builds a higher-level financial primitive on top of
    # the raw endpoints. They contain NO direct non-deterministic calls:
    # every external reading is obtained by calling another
    # BinanceConnector.get_* method (which runs its own equivalence-principle
    # block and returns the leader-agreed value to all nodes). The composite
    # then applies pure integer arithmetic, so the derived result is
    # reproduced identically on every validator.
    # ==================================================================

    @staticmethod
    def get_asset_price(
        asset: str, quote: str = "USDT", tolerance_bps: int = DEFAULT_TOLERANCE_BPS
    ) -> u256:
        """Price of one unit of ``asset`` denominated in ``quote`` (atto).

        Tries the direct market ``{asset}{quote}``; if Binance has no such
        symbol, falls back to the inverse market ``{quote}{asset}`` and
        returns the reciprocal. ``asset == quote`` returns exactly 1.0
        (``10**18``). This is the routing primitive behind :meth:`convert`
        and :meth:`value_basket`.
        """
        base = _clean_asset(asset)
        counter = _clean_asset(quote)
        _check_tolerance(tolerance_bps)
        if base == counter:
            return u256(ATTO_SCALE)
        try:
            return BinanceConnector.get_price_with_tolerance(base + counter, tolerance_bps)
        except gl.vm.UserError as exc:
            msg = getattr(exc, "message", None) or str(exc)
            if not msg.startswith(ERROR_EXTERNAL):
                raise  # transient / expected errors must not be masked
        # Direct market is unknown (deterministic 4xx): try the inverse market.
        try:
            inverse = int(
                BinanceConnector.get_price_with_tolerance(counter + base, tolerance_bps)
            )
        except gl.vm.UserError:
            raise gl.vm.UserError(
                f"{ERROR_EXTERNAL} No market path for {base}->{counter} "
                f"(tried {base}{counter} and {counter}{base})"
            )
        if inverse <= 0:
            raise gl.vm.UserError(
                f"{ERROR_EXTERNAL} Non-positive inverse price for {counter}{base}"
            )
        return u256((ATTO_SCALE * ATTO_SCALE) // inverse)

    @staticmethod
    def convert(
        from_asset: str,
        to_asset: str,
        amount_atto: int,
        quote: str = "USDT",
        tolerance_bps: int = DEFAULT_TOLERANCE_BPS,
    ) -> u256:
        """Convert ``amount_atto`` units of ``from_asset`` into ``to_asset``.

        Prices both legs against ``quote`` (default USDT, with inverse-market
        fallback) so **any token can be valued in any other**, even without a
        direct market. Returns the equivalent amount of ``to_asset`` (atto).
        """
        src = _clean_asset(from_asset)
        dst = _clean_asset(to_asset)
        _check_range(amount_atto, 0, _MAX_U256, "amount_atto")
        if src == dst:
            return u256(amount_atto)
        price_from = int(BinanceConnector.get_asset_price(src, quote, tolerance_bps))
        price_to = int(BinanceConnector.get_asset_price(dst, quote, tolerance_bps))
        if price_to <= 0:
            raise gl.vm.UserError(f"{ERROR_EXTERNAL} Non-positive price for {dst}")
        # amount_atto * (quote/src) / (quote/dst) -> dst units, atto-scaled.
        result = amount_atto * price_from // price_to
        if result > _MAX_U256:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Converted amount exceeds u256 range")
        return u256(result)

    @staticmethod
    def get_median_price(
        asset: str,
        quotes=None,
        min_sources: int = 2,
        tolerance_bps: int = DEFAULT_TOLERANCE_BPS,
    ) -> u256:
        """Manipulation-resistant USD price: median across stablecoin markets.

        Pulls the same ``asset`` from several independent quote markets
        (default ``USDT``/``USDC``/``FDUSD``) and returns the median, so a
        single stale or manipulated market cannot move the result. Markets
        that do not exist are skipped; at least ``min_sources`` must respond.
        """
        base = _clean_asset(asset)
        raw_quotes = list(_DEFAULT_MEDIAN_QUOTES) if quotes is None else _plain(quotes)
        markets = [_clean_asset(q) for q in raw_quotes]
        if not markets or len(markets) > 8:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} quotes must list 1..8 markets")
        _check_range(min_sources, 1, 8, "min_sources")
        min_sources = min(min_sources, len(markets))

        prices = []
        for market in markets:
            try:
                prices.append(
                    int(BinanceConnector.get_price_with_tolerance(base + market, tolerance_bps))
                )
            except gl.vm.UserError as exc:
                msg = getattr(exc, "message", None) or str(exc)
                if msg.startswith(ERROR_EXTERNAL):
                    continue  # market does not exist (deterministic) -> skip
                raise
        if len(prices) < min_sources:
            raise gl.vm.UserError(
                f"{ERROR_EXTERNAL} Only {len(prices)} of {len(markets)} {base} "
                f"markets available (need {min_sources})"
            )
        prices.sort()
        n = len(prices)
        mid = n // 2
        median = prices[mid] if n % 2 == 1 else (prices[mid - 1] + prices[mid]) // 2
        return u256(median)

    @staticmethod
    def value_basket(
        holdings, quote: str = "USDT", tolerance_bps: int = DEFAULT_TOLERANCE_BPS
    ) -> u256:
        """Total value of a basket ``{asset: amount_atto}`` in ``quote`` (atto).

        Prices an entire portfolio or index in a single call. Each leg is
        priced with :meth:`get_asset_price` (direct or inverse market).
        Capped at 20 legs to keep the per-transaction consensus cost bounded.
        """
        items = _plain(holdings)
        if not isinstance(items, dict) or not items or len(items) > 20:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} holdings must map 1..20 assets to atto amounts"
            )
        counter = _clean_asset(quote)
        total = 0
        for asset, amount in items.items():
            amount_atto = _check_range(int(amount), 0, _MAX_U256, f"amount for '{asset}'")
            price = int(BinanceConnector.get_asset_price(asset, counter, tolerance_bps))
            total += amount_atto * price // ATTO_SCALE
            if total > _MAX_U256:
                raise gl.vm.UserError(f"{ERROR_EXPECTED} Basket value exceeds u256 range")
        return u256(total)

    @staticmethod
    def is_price_safe(symbol: str, max_divergence_bps: int = 100) -> dict:
        """Cross-check spot vs 5-min average vs TWAP before settling.

        Returns ``{"safe", "spot_atto", "avg_atto", "twap_atto",
        "max_divergence_bps"}``. ``safe`` is True when all three readings
        agree within ``max_divergence_bps`` — a guard against settling on a
        single wicked / depegged / manipulated instantaneous price. Atto
        values are strings (they routinely exceed JS safe-integer range).
        """
        cleaned = _clean_symbol(symbol)
        _check_range(max_divergence_bps, 1, 10_000, "max_divergence_bps")
        spot = int(BinanceConnector.get_price_with_tolerance(cleaned))
        avg = int(BinanceConnector.get_avg_price(cleaned))
        twap = int(BinanceConnector.get_twap(cleaned, "1h", 24))
        readings = (spot, avg, twap)
        worst = 0
        for i in range(len(readings)):
            for j in range(i + 1, len(readings)):
                hi = max(readings[i], readings[j])
                if hi > 0:
                    divergence = abs(readings[i] - readings[j]) * 10_000 // hi
                    if divergence > worst:
                        worst = divergence
        return {
            "safe": worst <= max_divergence_bps,
            "spot_atto": str(spot),
            "avg_atto": str(avg),
            "twap_atto": str(twap),
            "max_divergence_bps": worst,
        }

    @staticmethod
    def check_peg(
        asset: str,
        quote: str = "USDT",
        target_atto: int = ATTO_SCALE,
        max_bps: int = 50,
        tolerance_bps: int = DEFAULT_TOLERANCE_BPS,
    ) -> dict:
        """Verify a stablecoin holds its peg.

        Returns ``{"pegged", "price_atto", "target_atto", "deviation_bps"}``.
        ``deviation_bps`` is ``|price - target| / target``. Defaults check that
        ``asset`` trades within 50 bps of 1.0 ``quote`` — a safety gate before
        accepting a stablecoin as collateral or settlement.
        """
        _check_range(target_atto, 1, _MAX_U256, "target_atto")
        _check_range(max_bps, 1, 10_000, "max_bps")
        price = int(BinanceConnector.get_asset_price(asset, quote, tolerance_bps))
        deviation_bps = abs(price - target_atto) * 10_000 // target_atto
        return {
            "pegged": deviation_bps <= max_bps,
            "price_atto": str(price),
            "target_atto": str(target_atto),
            "deviation_bps": deviation_bps,
        }

    @staticmethod
    def get_volatility(symbol: str, interval: str = "1h", periods: int = 24) -> dict:
        """Realized volatility from closed candles (pure integer math).

        Computes close-to-close returns in bps over the last ``periods``
        closed candles and returns their standard deviation. Output:
        ``{"volatility_bps", "mean_return_bps", "samples", "interval"}``.
        Useful for risk-based collateral, dynamic fees and insurance pricing.
        """
        cleaned = _clean_symbol(symbol)
        cleaned_interval = _clean_interval(interval)
        _check_range(periods, 2, 500, "periods")
        candles = _plain(BinanceConnector.get_klines(cleaned, cleaned_interval, periods))
        closes = [int(c["close_atto"]) for c in candles if int(c["close_atto"]) > 0]
        if len(closes) < 2:
            raise gl.vm.UserError(
                f"{ERROR_EXTERNAL} Not enough candles for volatility of {cleaned}"
            )
        returns = [
            (closes[i] - closes[i - 1]) * 10_000 // closes[i - 1]
            for i in range(1, len(closes))
        ]
        n = len(returns)
        mean = sum(returns) // n
        variance = sum((r - mean) ** 2 for r in returns) // n
        return {
            "volatility_bps": _isqrt(variance),
            "mean_return_bps": mean,
            "samples": n,
            "interval": cleaned_interval,
        }

    @staticmethod
    def get_trend(
        symbol: str,
        interval: str = "1h",
        periods: int = 24,
        flat_band_bps: int = 50,
    ) -> dict:
        """Simple moving-average trend signal.

        Compares the latest close to the SMA of the last ``periods`` closed
        candles. Returns ``{"trend", "last_atto", "sma_atto",
        "deviation_bps"}`` where ``trend`` is ``"up"``/``"down"``/``"flat"``
        (flat inside ±``flat_band_bps``). Drives auto-resolving
        "did X move up/down?" prediction markets.
        """
        cleaned = _clean_symbol(symbol)
        cleaned_interval = _clean_interval(interval)
        _check_range(periods, 2, 500, "periods")
        _check_range(flat_band_bps, 0, 10_000, "flat_band_bps")
        candles = _plain(BinanceConnector.get_klines(cleaned, cleaned_interval, periods))
        closes = [int(c["close_atto"]) for c in candles if int(c["close_atto"]) > 0]
        if not closes:
            raise gl.vm.UserError(f"{ERROR_EXTERNAL} No candle closes for {cleaned}")
        sma = sum(closes) // len(closes)
        last = closes[-1]
        deviation_bps = (last - sma) * 10_000 // sma if sma > 0 else 0
        if deviation_bps > flat_band_bps:
            trend = "up"
        elif deviation_bps < -flat_band_bps:
            trend = "down"
        else:
            trend = "flat"
        return {
            "trend": trend,
            "last_atto": str(last),
            "sma_atto": str(sma),
            "deviation_bps": deviation_bps,
        }

    @staticmethod
    def get_liquidity_score(symbol: str, levels: int = 500) -> dict:
        """Blend spread, top-book depth and 24h volume into a 0-100 score.

        A transparent, tunable health indicator (not an exchange metric):

        * spread component (0-40): 0 bps -> 40, ``>= _LIQ_SPREAD_MAX_BPS`` -> 0
        * depth component  (0-30): top-book quote depth vs ``_LIQ_DEPTH_FULL_QUOTE``
        * volume component (0-30): 24h quote volume vs ``_LIQ_VOLUME_FULL_QUOTE``

        Returns the score plus every input so callers can re-derive it. Run it
        before settling a large position to confirm the market can absorb it.
        """
        cleaned = _clean_symbol(symbol)
        book = _plain(BinanceConnector.get_order_book_summary(cleaned, levels))
        stats = _plain(BinanceConnector.get_24h_stats(cleaned))

        best_bid = int(book["best_bid_atto"])
        best_ask = int(book["best_ask_atto"])
        mid = (best_bid + best_ask) // 2
        spread_bps = ((best_ask - best_bid) * 10_000 // mid) if mid > 0 else _LIQ_SPREAD_MAX_BPS
        depth_quote = (int(book["bid_volume_atto"]) + int(book["ask_volume_atto"])) * mid // ATTO_SCALE
        volume_quote = int(stats["quote_volume_atto"])

        depth_units = depth_quote // ATTO_SCALE
        volume_units = volume_quote // ATTO_SCALE

        spread_score = max(0, 40 - (spread_bps * 40 // _LIQ_SPREAD_MAX_BPS))
        depth_score = min(30, depth_units * 30 // _LIQ_DEPTH_FULL_QUOTE)
        volume_score = min(30, volume_units * 30 // _LIQ_VOLUME_FULL_QUOTE)

        return {
            "score": spread_score + depth_score + volume_score,
            "spread_bps": spread_bps,
            "depth_quote_atto": str(depth_quote),
            "volume_24h_quote_atto": str(volume_quote),
            "components": {
                "spread": spread_score,
                "depth": depth_score,
                "volume": volume_score,
            },
        }

    @staticmethod
    def get_execution_price(
        symbol: str,
        side: str,
        base_amount_atto: int,
        levels: int = 100,
        tolerance_bps: int = 100,
    ) -> dict:
        """Volume-weighted fill price for a given order size (walks the book).

        ``side`` is ``"buy"`` (consume asks) or ``"sell"`` (consume bids).
        Walks up to ``levels`` of real order-book depth to compute the average
        price you would actually pay/receive for ``base_amount_atto`` base
        units — not the headline top-of-book price. Returns ``{"filled",
        "avg_price_atto", "filled_base_atto", "requested_base_atto",
        "quote_cost_atto", "levels_used"}``. Validators re-fetch; ``filled``
        must match and ``avg_price`` agree within ``tolerance_bps``.
        """
        cleaned = _clean_symbol(symbol)
        cleaned_side = str(side or "").strip().lower()
        if cleaned_side not in ("buy", "sell"):
            raise gl.vm.UserError(f"{ERROR_EXPECTED} side must be 'buy' or 'sell'")
        _check_range(base_amount_atto, 1, _MAX_U256, "base_amount_atto")
        _check_range(levels, 1, 5_000, "levels")
        _check_tolerance(tolerance_bps)
        url = f"{_BASE}/depth?symbol={cleaned}&limit={levels}"

        def fetch() -> dict:
            response = gl.nondet.web.get(url, headers=_JSON_HEADERS)
            payload = _expect_object(response, url)
            side_key = "asks" if cleaned_side == "buy" else "bids"
            book = _field(payload, side_key, url)
            if not isinstance(book, list) or not book:
                raise gl.vm.UserError(f"{ERROR_EXTERNAL} Empty {side_key} from {url}")
            remaining = base_amount_atto
            quote_cost = 0
            filled_base = 0
            used = 0
            for level in book:
                price_atto = to_atto(str(level[0]))
                qty_atto = to_atto(str(level[1]))
                used += 1
                take = qty_atto if qty_atto < remaining else remaining
                quote_cost += take * price_atto // ATTO_SCALE
                filled_base += take
                remaining -= take
                if remaining <= 0:
                    break
            avg_price = (quote_cost * ATTO_SCALE // filled_base) if filled_base > 0 else 0
            return {
                "filled": remaining <= 0,
                "avg_price_atto": avg_price,
                "filled_base_atto": filled_base,
                "requested_base_atto": base_amount_atto,
                "quote_cost_atto": quote_cost,
                "levels_used": used,
            }

        def agree(theirs, mine) -> bool:
            if not isinstance(theirs, dict) or not isinstance(mine, dict):
                return False
            if bool(theirs.get("filled")) != bool(mine.get("filled")):
                return False
            return _within_bps(
                int(theirs.get("avg_price_atto", -1)),
                int(mine.get("avg_price_atto", -2)),
                tolerance_bps,
            )

        return gl.vm.run_nondet_unsafe(fetch, _make_validator(fetch, agree))

    @staticmethod
    def price_at(symbol: str, timestamp_ms: int, interval: str = "1m") -> u256:
        """Byte-exact closing price at a past ``timestamp_ms`` (epoch ms).

        Pins the kline window's ``endTime`` so the result is fully
        deterministic (``strict_eq``): every validator fetches the identical
        immutable candle. Returns the close of the candle covering
        ``timestamp_ms``. Use a **past** timestamp — a still-open candle may
        fail consensus and rotate the leader. Ideal for settling options /
        bets at a fixed expiry against a tamper-proof historical price.
        """
        cleaned = _clean_symbol(symbol)
        cleaned_interval = _clean_interval(interval)
        _check_range(timestamp_ms, 1, 4_102_444_800_000, "timestamp_ms")  # <= year 2100
        url = (
            f"{_BASE}/klines?symbol={cleaned}&interval={cleaned_interval}"
            f"&endTime={timestamp_ms}&limit=1"
        )

        def fetch() -> int:
            response = gl.nondet.web.get(url, headers=_JSON_HEADERS)
            rows = _expect_array(response, url)
            if not rows:
                raise gl.vm.UserError(
                    f"{ERROR_EXTERNAL} No candle at {timestamp_ms} for {cleaned}"
                )
            candles = _parse_klines_payload(rows, url)
            return int(candles[-1]["close_atto"])

        return u256(gl.eq_principle.strict_eq(fetch))
