"""
╔══════════════════════════════════════════════════════════════════╗
║  HYPERLIQUID API — Real-time derivatives data for MM signals     ║
║                                                                  ║
║  Provides:                                                       ║
║    - Oracle BTC price (independent of Binance)                   ║
║    - Funding rate (hourly, shows crowd positioning)               ║
║    - Open interest (total leveraged exposure)                    ║
║    - CVD from trade flow (buy vs sell volume imbalance)           ║
║    - Liquidation proxy (trade cluster detection)                 ║
║                                                                  ║
║  All endpoints are public — no API key needed.                   ║
║                                                                  ║
║  Usage:                                                          ║
║    feed = HyperliquidFeed()                                      ║
║    await feed.start()                                            ║
║    snapshot = feed.get_snapshot()  # returns SideDataSnapshot     ║
║    await feed.stop()                                             ║
╚══════════════════════════════════════════════════════════════════╝
"""

import asyncio
import json
import math
import time
from collections import deque
from dataclasses import dataclass
from typing import Optional

import aiohttp
import websockets
from loguru import logger


# ═══════════════════════════════════════════════════════════
#  CONSTANTS
# ═══════════════════════════════════════════════════════════

HL_REST_URL = "https://api.hyperliquid.xyz/info"
HL_WS_URL = "wss://api.hyperliquid.xyz/ws"

# Signal normalization parameters (tuned for BTC perps)
FUNDING_NORM = 0.0003      # funding_rate / this → tanh input. 0.03%/hr maps to ~0.71
OI_CHANGE_NORM = 0.005     # 0.5% OI change in 15 min maps to ~0.71
CVD_AMPLIFIER = 3.0        # amplify normalized CVD before tanh
LIQ_CLUSTER_WINDOW_MS = 500    # ms window to detect liquidation clusters
LIQ_CLUSTER_MIN_TRADES = 5    # minimum trades in window to flag as liquidation
LIQ_SIZE_THRESHOLD_MULT = 2.0 # size must be 2x rolling avg to count

# Rolling window sizes
CVD_WINDOW_SECONDS = 300       # 5-minute CVD window
OI_HISTORY_SECONDS = 900       # 15-minute OI change tracking
TRADE_SIZE_HISTORY = 500       # rolling window for avg trade size


# ═══════════════════════════════════════════════════════════
#  DATA STRUCTURES
# ═══════════════════════════════════════════════════════════

@dataclass
class TradeEvent:
    """Single trade from the Hyperliquid WebSocket feed."""
    timestamp_ms: int
    side: str       # "A" (sell/ask) or "B" (buy/bid)
    price: float
    size: float     # in contracts


@dataclass
class HLMarketData:
    """Latest REST polling data for BTC perp."""
    oracle_price: float = 0.0
    mark_price: float = 0.0
    funding_rate: float = 0.0      # hourly rate as decimal (e.g., 0.0001 = 0.01%/hr)
    open_interest: float = 0.0     # in USD
    day_volume: float = 0.0        # 24h notional volume
    timestamp: float = 0.0


# ═══════════════════════════════════════════════════════════
#  REST POLLER — oracle price, funding, OI (every 3 seconds)
# ═══════════════════════════════════════════════════════════

class HyperliquidRestPoller:
    """
    Polls Hyperliquid's info endpoint for BTC perpetual market data.

    The metaAndAssetCtxs endpoint returns metadata and context for all
    listed assets in a single call. We extract BTC's entry and pull:
      - oraclePx: Hyperliquid's oracle price (aggregated from CEXes)
      - funding: current hourly funding rate
      - openInterest: total open interest in contracts
      - markPx: mark price (oracle-based, used for liquidations)
      - dayNtlVlm: 24-hour notional volume
    """

    def __init__(self, poll_interval: float = 3.0):
        self.poll_interval = poll_interval
        self.data = HLMarketData()
        self._oi_history: deque = deque(maxlen=int(OI_HISTORY_SECONDS / poll_interval))
        self._session: Optional[aiohttp.ClientSession] = None
        self._task: Optional[asyncio.Task] = None
        self._running = False

    async def start(self):
        """Start the polling loop."""
        self._session = aiohttp.ClientSession()
        self._running = True
        self._task = asyncio.create_task(self._poll_loop())
        logger.info("HyperliquidRestPoller started (interval={}s)", self.poll_interval)

    async def stop(self):
        """Stop polling and close session."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._session:
            await self._session.close()
        logger.info("HyperliquidRestPoller stopped")

    async def _poll_loop(self):
        """Main polling loop — fetches data every poll_interval seconds."""
        while self._running:
            try:
                await self._fetch_data()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("HL REST poll error: {}", e)
            await asyncio.sleep(self.poll_interval)

    async def _fetch_data(self):
        """Fetch metaAndAssetCtxs and extract BTC data."""
        payload = {"type": "metaAndAssetCtxs"}
        async with self._session.post(HL_REST_URL, json=payload) as resp:
            if resp.status != 200:
                logger.warning("HL REST status {}", resp.status)
                return
            result = await resp.json()

        # Response format: [meta_info, [asset_ctx_0, asset_ctx_1, ...]]
        # meta_info contains universe with asset names
        # asset_ctxs are in the same order as meta_info.universe
        if not isinstance(result, list) or len(result) < 2:
            return

        meta = result[0]
        asset_ctxs = result[1]

        # Find BTC's index in the universe
        btc_idx = None
        for i, asset in enumerate(meta.get("universe", [])):
            if asset.get("name") == "BTC":
                btc_idx = i
                break

        if btc_idx is None or btc_idx >= len(asset_ctxs):
            logger.warning("BTC not found in HL universe")
            return

        ctx = asset_ctxs[btc_idx]
        now = time.time()

        self.data = HLMarketData(
            oracle_price=float(ctx.get("oraclePx", "0")),
            mark_price=float(ctx.get("markPx", "0")),
            funding_rate=float(ctx.get("funding", "0")),
            open_interest=float(ctx.get("openInterest", "0")),
            day_volume=float(ctx.get("dayNtlVlm", "0")),
            timestamp=now,
        )

        # Track OI history for rate-of-change computation
        self._oi_history.append((now, self.data.open_interest))

    def get_oi_change_pct(self) -> float:
        """Compute OI percentage change over the last 15 minutes."""
        if len(self._oi_history) < 2:
            return 0.0
        oldest_time, oldest_oi = self._oi_history[0]
        current_oi = self.data.open_interest
        if oldest_oi <= 0:
            return 0.0
        return (current_oi - oldest_oi) / oldest_oi


# ═══════════════════════════════════════════════════════════
#  WEBSOCKET FEED — trade-by-trade for CVD + liquidation proxy
# ═══════════════════════════════════════════════════════════

class HyperliquidWSFeed:
    """
    WebSocket connection for BTC trade flow data.

    Subscribes to the "trades" channel for BTC. Each message contains
    one or more trade events with side, price, and size.

    Computes:
      - CVD (Cumulative Volume Delta): rolling sum of (buy_vol - sell_vol)
      - Liquidation proxy: detects clusters of large same-direction trades
        within a short time window, which typically indicate forced liquidations
    """

    def __init__(self):
        self._trades: deque = deque(maxlen=10000)  # recent trades for CVD
        self._trade_sizes: deque = deque(maxlen=TRADE_SIZE_HISTORY)  # for avg size
        self._ws = None
        self._task: Optional[asyncio.Task] = None
        self._running = False

        # CVD accumulators (rolling 5-minute window)
        self._buy_vol_window: deque = deque()   # (timestamp_ms, size)
        self._sell_vol_window: deque = deque()

        # Liquidation detection state
        self._liq_intensity: float = 0.0   # decays over time
        self._liq_direction: float = 0.0   # positive=short liqs, negative=long liqs
        self._last_liq_decay: float = 0.0

    async def start(self):
        """Start the WebSocket connection."""
        self._running = True
        self._last_liq_decay = time.time()
        self._task = asyncio.create_task(self._ws_loop())
        logger.info("HyperliquidWSFeed started")

    async def stop(self):
        """Stop the WebSocket connection."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._ws:
            await self._ws.close()
        logger.info("HyperliquidWSFeed stopped")

    async def _ws_loop(self):
        """Reconnecting WebSocket loop."""
        while self._running:
            try:
                async with websockets.connect(HL_WS_URL, ping_interval=20) as ws:
                    self._ws = ws
                    # Subscribe to BTC trades
                    sub_msg = {
                        "method": "subscribe",
                        "subscription": {"type": "trades", "coin": "BTC"}
                    }
                    await ws.send(json.dumps(sub_msg))
                    logger.info("HL WebSocket connected, subscribed to BTC trades")

                    async for raw in ws:
                        if not self._running:
                            break
                        try:
                            msg = json.loads(raw)
                            self._process_message(msg)
                        except json.JSONDecodeError:
                            continue

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("HL WebSocket error: {} — reconnecting in 5s", e)
                await asyncio.sleep(5)

    def _process_message(self, msg: dict):
        """Process a trades WebSocket message."""
        # Message format: {"channel": "trades", "data": [{"coin": "BTC", "side": "B", "px": "...", "sz": "...", "time": ..., "tid": ...}, ...]}
        if msg.get("channel") != "trades":
            return

        trades = msg.get("data", [])
        now_ms = int(time.time() * 1000)

        for t in trades:
            if t.get("coin") != "BTC":
                continue

            trade = TradeEvent(
                timestamp_ms=t.get("time", now_ms),
                side=t.get("side", ""),
                price=float(t.get("px", "0")),
                size=float(t.get("sz", "0")),
            )

            self._trades.append(trade)
            self._trade_sizes.append(trade.size)

            # Accumulate into buy/sell volume windows
            if trade.side == "B":  # buy (bid taker)
                self._buy_vol_window.append((trade.timestamp_ms, trade.size))
            elif trade.side == "A":  # sell (ask taker)
                self._sell_vol_window.append((trade.timestamp_ms, trade.size))

        # Prune old entries from CVD windows
        cutoff_ms = now_ms - (CVD_WINDOW_SECONDS * 1000)
        while self._buy_vol_window and self._buy_vol_window[0][0] < cutoff_ms:
            self._buy_vol_window.popleft()
        while self._sell_vol_window and self._sell_vol_window[0][0] < cutoff_ms:
            self._sell_vol_window.popleft()

        # Detect liquidation clusters
        self._detect_liquidations(trades, now_ms)

        # Decay liquidation intensity over time
        now = time.time()
        if now - self._last_liq_decay > 1.0:
            elapsed = now - self._last_liq_decay
            decay = math.exp(-elapsed / 30.0)  # 30-second half-life
            self._liq_intensity *= decay
            self._liq_direction *= decay
            self._last_liq_decay = now

    def _detect_liquidations(self, raw_trades: list, now_ms: int):
        """
        Detect liquidation-like activity from trade clusters.

        A liquidation typically appears as a rapid burst of same-direction
        trades at or near market price. We detect this by looking for
        clusters of 5+ trades within 500ms on the same side with above-
        average total size.
        """
        if len(raw_trades) < LIQ_CLUSTER_MIN_TRADES:
            return

        # Check if this batch of trades is a cluster (same side, rapid fire)
        btc_trades = [t for t in raw_trades if t.get("coin") == "BTC"]
        if len(btc_trades) < LIQ_CLUSTER_MIN_TRADES:
            return

        sides = [t.get("side") for t in btc_trades]
        dominant_side = max(set(sides), key=sides.count)
        same_side_count = sides.count(dominant_side)

        if same_side_count < LIQ_CLUSTER_MIN_TRADES:
            return

        # Check total size against rolling average
        cluster_size = sum(
            float(t.get("sz", "0")) for t in btc_trades
            if t.get("side") == dominant_side
        )

        if self._trade_sizes:
            avg_size = sum(self._trade_sizes) / len(self._trade_sizes)
            if cluster_size < avg_size * LIQ_SIZE_THRESHOLD_MULT:
                return
        else:
            return  # not enough history yet

        # This looks like a liquidation — record it
        # Positive intensity = short liquidation (price pushed UP, buyers dominate)
        # Negative intensity = long liquidation (price pushed DOWN, sellers dominate)
        direction = 1.0 if dominant_side == "B" else -1.0
        intensity = cluster_size / max(avg_size, 1e-6)

        self._liq_intensity += intensity
        self._liq_direction += direction * intensity

        logger.debug(
            "Liquidation cluster detected: {} trades, {:.2f} contracts, side={}",
            same_side_count, cluster_size, dominant_side,
        )

    def get_cvd_signal(self) -> float:
        """
        Compute normalized CVD signal in [-1, +1].

        CVD = cumulative(buy_volume) - cumulative(sell_volume) over 5 minutes.
        Normalized by total volume, then amplified and bounded with tanh.

        Positive = net buying pressure (bullish flow)
        Negative = net selling pressure (bearish flow)
        """
        buy_vol = sum(sz for _, sz in self._buy_vol_window)
        sell_vol = sum(sz for _, sz in self._sell_vol_window)
        total_vol = buy_vol + sell_vol

        if total_vol < 1e-6:
            return 0.0

        cvd_normalized = (buy_vol - sell_vol) / total_vol  # [-1, +1]
        return math.tanh(cvd_normalized * CVD_AMPLIFIER)

    def get_liq_signal(self) -> float:
        """
        Get liquidation pressure signal in [-1, +1].

        Positive = short liquidations (bullish pressure)
        Negative = long liquidations (bearish pressure)
        """
        if self._liq_intensity < 0.01:
            return 0.0

        # Normalize direction by intensity
        raw = self._liq_direction / max(self._liq_intensity, 1e-6)
        return math.tanh(raw * self._liq_intensity * 0.1)


# ═══════════════════════════════════════════════════════════
#  UNIFIED FEED — combines REST + WS into a single interface
# ═══════════════════════════════════════════════════════════

class HyperliquidFeed:
    """
    Unified interface for all Hyperliquid data.

    Combines the REST poller (oracle, funding, OI) and WebSocket feed
    (CVD, liquidation proxy) into a single start/stop/get_snapshot API.

    Usage:
        feed = HyperliquidFeed()
        await feed.start()

        # Every quoting cycle:
        snapshot = feed.get_snapshot()
        # snapshot has: oracle_price, funding_rate, open_interest,
        #               cvd_signal, liq_signal, funding_signal, oi_signal

        await feed.stop()
    """

    def __init__(self, poll_interval: float = 3.0):
        self._rest = HyperliquidRestPoller(poll_interval=poll_interval)
        self._ws = HyperliquidWSFeed()

    async def start(self):
        """Start both REST poller and WebSocket feed."""
        await asyncio.gather(self._rest.start(), self._ws.start())
        logger.info("HyperliquidFeed fully started (REST + WS)")

    async def stop(self):
        """Stop both feeds."""
        await asyncio.gather(self._rest.stop(), self._ws.stop())
        logger.info("HyperliquidFeed stopped")

    @property
    def oracle_price(self) -> float:
        return self._rest.data.oracle_price

    @property
    def funding_rate(self) -> float:
        return self._rest.data.funding_rate

    @property
    def open_interest(self) -> float:
        return self._rest.data.open_interest

    def get_funding_signal(self) -> float:
        """
        Normalize funding rate to [-1, +1].

        Normal BTC funding: ~0.001% to 0.01%/hr
        Extreme: > 0.05%/hr

        Positive signal = longs paying shorts (bullish crowding)
        Negative signal = shorts paying longs (bearish crowding)
        """
        return math.tanh(self._rest.data.funding_rate / FUNDING_NORM)

    def get_oi_signal(self) -> float:
        """
        Normalize OI rate of change to [-1, +1].

        Positive = OI growing (new positions entering)
        Negative = OI shrinking (positions closing)
        """
        oi_change = self._rest.get_oi_change_pct()
        return math.tanh(oi_change / OI_CHANGE_NORM)

    def get_cvd_signal(self) -> float:
        """Get CVD signal from WebSocket trade flow."""
        return self._ws.get_cvd_signal()

    def get_liq_signal(self) -> float:
        """Get liquidation pressure signal from trade clusters."""
        return self._ws.get_liq_signal()

    def get_snapshot_fields(self) -> dict:
        """
        Get all Hyperliquid fields as a dict, ready to populate SideDataSnapshot.

        Returns keys matching SideDataSnapshot field names so you can do:
            snapshot = SideDataSnapshot(**feed.get_snapshot_fields(), **other_fields)
        """
        return {
            "hl_oracle_price": self._rest.data.oracle_price,
            "hl_funding_rate": self._rest.data.funding_rate,
            "hl_open_interest": self._rest.data.open_interest,
            "cvd_signal": self.get_cvd_signal(),
            "liq_signal": self.get_liq_signal(),
            "funding_signal": self.get_funding_signal(),
            "oi_signal": self.get_oi_signal(),
        }

    @property
    def is_connected(self) -> bool:
        """True if both REST and WS are producing data."""
        has_rest = self._rest.data.timestamp > 0
        has_ws = len(self._ws._trades) > 0
        return has_rest and has_ws

    def status(self) -> dict:
        """Diagnostic status for logging/dashboard."""
        return {
            "rest_connected": self._rest.data.timestamp > 0,
            "rest_last_update": self._rest.data.timestamp,
            "ws_trade_count": len(self._ws._trades),
            "oracle_price": self._rest.data.oracle_price,
            "funding_rate": f"{self._rest.data.funding_rate:.6f}",
            "open_interest": f"{self._rest.data.open_interest:,.0f}",
            "cvd_signal": f"{self.get_cvd_signal():+.3f}",
            "liq_signal": f"{self.get_liq_signal():+.3f}",
            "funding_signal": f"{self.get_funding_signal():+.3f}",
            "oi_signal": f"{self.get_oi_signal():+.3f}",
        }
