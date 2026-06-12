# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
"""
PriceOracle — example Intelligent Contract for ``GenFeed``.

Demonstrates how to consume the Binance connector family from
``@gl.public.write`` methods and persist atto-scaled results in typed on-chain
storage. Every value stored by this contract was agreed upon by GenLayer
validators through an equivalence principle — no single node can forge a
reading.

Showcased connector calls:
    * ``get_price`` / ``get_price_with_tolerance`` — spot price feeds
    * ``get_twap``               — manipulation-resistant settlement price
    * ``get_24h_stats``          — daily high/low/volume snapshot
    * ``get_book_ticker``        — live bid/ask spread
    * ``get_server_time``        — decentralized clock heartbeat
    * ``get_median_price``       — manipulation-resistant cross-market price
    * ``is_price_safe``          — spot/avg/TWAP divergence guard before settling

Storage conventions:
    * All prices are atto-scaled integers (value * 10**18), keyed by market
      symbol (``"BTCUSDT"``).

Deployment: this file imports the library as a Python module. Deploy it either
as a multi-file contract package, or run ``python scripts/bundle.py`` to produce
``dist/price_oracle.bundle.py`` — a single self-contained file ready for any
GenLayer deployment tool (see README).
"""

from genlayer import *

from genfeed import BinanceConnector, ERROR_EXPECTED


class PriceOracle(gl.Contract):
    """On-chain market-data cache fed exclusively by validator-agreed reads."""

    # ---- typed persistent storage (class-level annotations = storage slots) ----
    owner: Address
    prices_atto: TreeMap[str, u256]      # "BTCUSDT" -> spot price * 10**18
    twaps_atto: TreeMap[str, u256]       # "BTCUSDT" -> 24x1h TWAP * 10**18
    daily_high_atto: TreeMap[str, u256]  # 24h high * 10**18
    daily_low_atto: TreeMap[str, u256]   # 24h low * 10**18
    spreads_bps: TreeMap[str, u256]      # current bid/ask spread in bps
    symbols: DynArray[str]               # insertion-ordered registry of symbols
    last_heartbeat_ms: u256              # Binance server time at last heartbeat
    total_updates: u256                  # lifetime number of oracle writes

    def __init__(self):
        self.owner = gl.message.sender_address
        self.last_heartbeat_ms = u256(0)
        self.total_updates = u256(0)

    # ------------------------------------------------------------------
    # Write methods — each one performs a consensus-validated external read
    # ------------------------------------------------------------------

    @gl.public.write
    def update_price(self, symbol: str) -> None:
        """Fetch a spot price under byte-exact consensus (strict_eq)."""
        key = symbol.strip().upper()
        self._register(key)
        self.prices_atto[key] = BinanceConnector.get_price(symbol)
        self._bump()

    @gl.public.write
    def update_price_tolerant(self, symbol: str, tolerance_bps: int) -> None:
        """Fast-market spot price: validators accept a small bps spread."""
        key = symbol.strip().upper()
        self._register(key)
        self.prices_atto[key] = BinanceConnector.get_price_with_tolerance(
            symbol, tolerance_bps
        )
        self._bump()

    @gl.public.write
    def update_twap(self, symbol: str) -> None:
        """Store the 24x1h TWAP — the manipulation-resistant settlement price."""
        key = symbol.strip().upper()
        self._register(key)
        self.twaps_atto[key] = BinanceConnector.get_twap(symbol, "1h", 24)
        self._bump()

    @gl.public.write
    def update_daily_range(self, symbol: str) -> None:
        """Snapshot the 24h high/low from the full statistics endpoint."""
        key = symbol.strip().upper()
        self._register(key)
        stats = BinanceConnector.get_24h_stats(symbol)
        self.daily_high_atto[key] = u256(int(stats["high_atto"]))
        self.daily_low_atto[key] = u256(int(stats["low_atto"]))
        self._bump()

    @gl.public.write
    def update_spread(self, symbol: str) -> None:
        """Store the current bid/ask spread (bps) from the book ticker."""
        key = symbol.strip().upper()
        self._register(key)
        book = BinanceConnector.get_book_ticker(symbol)
        self.spreads_bps[key] = u256(int(book["spread_bps"]))
        self._bump()

    @gl.public.write
    def heartbeat(self) -> None:
        """Record Binance server time — a free decentralized clock source."""
        self.last_heartbeat_ms = BinanceConnector.get_server_time()
        self._bump()

    # ------------------------------------------------------------------
    # Composite "super-power" writes — derived primitives in one call
    # ------------------------------------------------------------------

    @gl.public.write
    def update_robust_price(self, asset: str) -> None:
        """Store a manipulation-resistant USD price (median across markets).

        Pass a bare asset such as ``"BTC"``; the connector pulls it from
        several stablecoin markets and stores the median, so no single market
        can move the recorded price.
        """
        key = asset.strip().upper()
        self._register(key)
        self.prices_atto[key] = BinanceConnector.get_median_price(asset)
        self._bump()

    @gl.public.write
    def settle_if_safe(self, symbol: str) -> None:
        """Record the TWAP settlement price only if spot/avg/TWAP all agree.

        Guards against settling on a wicked, depegged or manipulated price:
        if the three readings diverge beyond the safety band, the write
        reverts instead of locking a bad price on-chain.
        """
        report = BinanceConnector.is_price_safe(symbol)
        if not report["safe"]:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} Price unsafe to settle: "
                f"{report['max_divergence_bps']} bps divergence"
            )
        key = symbol.strip().upper()
        self._register(key)
        self.twaps_atto[key] = u256(int(report["twap_atto"]))
        self._bump()

    # ------------------------------------------------------------------
    # View methods — free reads for frontends and other contracts
    # ------------------------------------------------------------------

    @gl.public.view
    def get_price(self, symbol: str) -> u256:
        """Stored spot price for ``symbol`` (atto). Raises if never fetched."""
        key = symbol.strip().upper()
        price = self.prices_atto.get(key)
        if price is None:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} No stored price for '{key}'")
        return price

    @gl.public.view
    def get_twap(self, symbol: str) -> u256:
        """Stored TWAP for ``symbol`` (atto). Raises if never fetched."""
        key = symbol.strip().upper()
        twap = self.twaps_atto.get(key)
        if twap is None:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} No stored TWAP for '{key}'")
        return twap

    @gl.public.view
    def get_market_snapshot(self, symbol: str) -> dict:
        """Everything stored about a symbol, JSON-friendly.

        Atto values are returned as strings because they routinely exceed the
        safe-integer range of JSON consumers.
        """
        key = symbol.strip().upper()

        def _str_or_none(table) -> str | None:
            value = table.get(key)
            return None if value is None else str(int(value))

        return {
            "symbol": key,
            "price_atto": _str_or_none(self.prices_atto),
            "twap_atto": _str_or_none(self.twaps_atto),
            "daily_high_atto": _str_or_none(self.daily_high_atto),
            "daily_low_atto": _str_or_none(self.daily_low_atto),
            "spread_bps": _str_or_none(self.spreads_bps),
        }

    @gl.public.view
    def get_all_prices(self) -> dict:
        """Snapshot of every tracked symbol's spot price (atto, as strings)."""
        out: dict = {}
        for i in range(len(self.symbols)):
            key = self.symbols[i]
            price = self.prices_atto.get(key)
            if price is not None:
                out[key] = str(int(price))
        return out

    @gl.public.view
    def get_stats(self) -> dict:
        """Operational counters for monitoring dashboards."""
        return {
            "tracked_symbols": len(self.symbols),
            "total_updates": int(self.total_updates),
            "last_heartbeat_ms": str(int(self.last_heartbeat_ms)),
        }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _register(self, key: str) -> None:
        """Add ``key`` to the symbol registry on first sight (O(n), small n)."""
        for i in range(len(self.symbols)):
            if self.symbols[i] == key:
                return
        self.symbols.append(key)

    def _bump(self) -> None:
        """Increment the lifetime write counter."""
        self.total_updates = u256(int(self.total_updates) + 1)
