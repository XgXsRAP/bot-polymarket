"""
Binance BTC/USDT real-time price feed.

Provides rolling price history for computing:
  - btc_price:        Latest spot price
  - btc_change_1m:    % change over last 60 seconds
  - btc_change_5m:    % change over last 300 seconds
  - btc_volatility_1m: Std dev of per-second returns over last 60 seconds

Connection chain: WebSocket (primary) → REST bootstrap (immediate on start).
Auto-reconnects with 5s delay on disconnect.
"""

import asyncio
import json
import math
import time
from collections import deque

import aiohttp
import websockets
from loguru import logger

_WS_URL = "wss://stream.binance.com:9443/ws/btcusdt@aggTrade"
_REST_URL = "https://api.binance.com/api/v3/ticker/price"

# Kraken fallback (used when Binance is geo-restricted, e.g. HTTP 451)
_KRAKEN_REST_URL = "https://api.kraken.com/0/public/Ticker"
_KRAKEN_WS_URL = "wss://ws.kraken.com/v2"
_HISTORY_SECONDS = 310  # slightly over 5 min to cover change_5m


class BinanceBTCFeed:
    """
    Real-time BTC/USDT price from Binance aggTrade stream.

    Usage:
        feed = BinanceBTCFeed()
        await feed.start()
        fields = feed.get_snapshot_fields()  # dict ready for SideDataSnapshot
        await feed.stop()
    """

    def __init__(self):
        self._history: deque[tuple[float, float]] = deque()  # (timestamp, price)
        self._latest_price: float = 0.0
        self._running = False
        self._connected = False
        self._task: asyncio.Task | None = None

    # ── Lifecycle ───────────────────────────────────────────────────────────

    async def start(self):
        """Start feed: REST bootstrap first, then persistent WebSocket."""
        self._running = True
        await self._fetch_rest_price()
        self._task = asyncio.create_task(self._ws_loop())

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._connected = False

    # ── Data fetching ────────────────────────────────────────────────────────

    async def _fetch_rest_price(self):
        """Bootstrap price: try Binance first, fall back to Kraken."""
        # Try Binance
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    _REST_URL,
                    params={"symbol": "BTCUSDT"},
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        self._record(float(data["price"]))
                        logger.info(f"Binance REST bootstrap: BTC=${self._latest_price:,.2f}")
                        return
        except Exception as e:
            logger.warning(f"Binance REST bootstrap failed: {e}")

        # Kraken fallback
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    _KRAKEN_REST_URL,
                    params={"pair": "XBTUSD"},
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    data = await resp.json()
                    price = float(data["result"]["XXBTZUSD"]["c"][0])
                    self._record(price)
                    logger.info(f"Kraken REST bootstrap: BTC=${self._latest_price:,.2f}")
        except Exception as e:
            logger.warning(f"Kraken REST bootstrap failed: {e}")

    async def _ws_loop(self):
        """Maintain persistent WebSocket — try Binance first, fall back to Kraken."""
        while self._running:
            try:
                async with websockets.connect(
                    _WS_URL,
                    ping_interval=20,
                    ping_timeout=10,
                ) as ws:
                    self._connected = True
                    logger.info("Binance WebSocket connected (BTC aggTrade)")
                    async for raw in ws:
                        if not self._running:
                            break
                        try:
                            msg = json.loads(raw)
                            self._record(float(msg["p"]))  # "p" = price in aggTrade
                        except (KeyError, ValueError, json.JSONDecodeError):
                            pass
            except Exception as e:
                self._connected = False
                if self._running:
                    logger.warning(f"Binance WS disconnected ({e}). Trying Kraken fallback...")
                    await self._kraken_ws_loop()

    async def _kraken_ws_loop(self):
        """Kraken WebSocket fallback for geo-restricted environments."""
        while self._running:
            try:
                async with websockets.connect(
                    _KRAKEN_WS_URL,
                    ping_interval=20,
                    ping_timeout=10,
                ) as ws:
                    sub = {
                        "method": "subscribe",
                        "params": {"channel": "trade", "symbol": ["BTC/USD"]},
                    }
                    await ws.send(json.dumps(sub))
                    self._connected = True
                    logger.info("Kraken WebSocket connected (BTC/USD trade feed)")
                    async for raw in ws:
                        if not self._running:
                            break
                        try:
                            msg = json.loads(raw)
                            # Kraken v2 trade message: {"channel":"trade","type":"update","data":[{"price":...},...]}
                            if msg.get("channel") == "trade" and msg.get("type") in ("update", "snapshot"):
                                for t in msg.get("data", []):
                                    self._record(float(t["price"]))
                        except (KeyError, ValueError, json.JSONDecodeError):
                            pass
            except Exception as e:
                self._connected = False
                if self._running:
                    logger.warning(f"Kraken WS disconnected ({e}). Reconnecting in 5s...")
                    await asyncio.sleep(5)
                    # Try Binance again after reconnect pause
                    return

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _record(self, price: float):
        """Append a price tick and prune history older than the window."""
        now = time.time()
        self._history.append((now, price))
        self._latest_price = price
        cutoff = now - _HISTORY_SECONDS
        while self._history and self._history[0][0] < cutoff:
            self._history.popleft()

    def _price_n_seconds_ago(self, seconds: float) -> float | None:
        """Return the price reading closest to `seconds` seconds ago, or None."""
        if not self._history:
            return None
        target_ts = time.time() - seconds
        best_price = None
        best_delta = float("inf")
        for ts, price in self._history:
            delta = abs(ts - target_ts)
            if delta < best_delta:
                best_delta = delta
                best_price = price
            if ts > target_ts + best_delta:
                # History is time-ordered; stop once we've passed the optimal point
                break
        return best_price

    # ── Public properties ────────────────────────────────────────────────────

    @property
    def price(self) -> float:
        return self._latest_price

    @property
    def change_1m(self) -> float:
        """Fractional return over last 60 s. e.g. 0.005 = +0.5%."""
        p_now = self._latest_price
        p_past = self._price_n_seconds_ago(60)
        if not p_now or not p_past:
            return 0.0
        return (p_now - p_past) / p_past

    @property
    def change_5m(self) -> float:
        """Fractional return over last 300 s."""
        p_now = self._latest_price
        p_past = self._price_n_seconds_ago(300)
        if not p_now or not p_past:
            return 0.0
        return (p_now - p_past) / p_past

    @property
    def volatility_1m(self) -> float:
        """
        Std dev of per-tick returns over the last 60 seconds.
        Defaults to 0.001 until sufficient history is available.
        """
        now = time.time()
        recent = [(ts, px) for ts, px in self._history if ts >= now - 60]
        if len(recent) < 5:
            return 0.001
        returns = []
        for i in range(1, len(recent)):
            p0, p1 = recent[i - 1][1], recent[i][1]
            if p0 > 0:
                returns.append((p1 - p0) / p0)
        if len(returns) < 2:
            return 0.001
        mean = sum(returns) / len(returns)
        variance = sum((r - mean) ** 2 for r in returns) / len(returns)
        return max(math.sqrt(variance), 1e-6)

    @property
    def is_connected(self) -> bool:
        return self._connected

    def get_snapshot_fields(self) -> dict:
        """Returns dict matching the BTC fields of SideDataSnapshot."""
        return {
            "btc_price": self.price,
            "btc_change_1m": self.change_1m,
            "btc_change_5m": self.change_5m,
            "btc_volatility_1m": self.volatility_1m,
        }

    def status(self) -> dict:
        return {
            "connected": self._connected,
            "price": f"${self._latest_price:,.2f}" if self._latest_price else "N/A",
            "change_1m": f"{self.change_1m * 100:+.3f}%",
            "change_5m": f"{self.change_5m * 100:+.3f}%",
            "volatility_1m": f"{self.volatility_1m:.5f}",
            "history_ticks": len(self._history),
        }
