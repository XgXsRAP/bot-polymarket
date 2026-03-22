"""
╔══════════════════════════════════════════════════════════╗
║     POLYMARKET BTC HIGH-FREQUENCY TRADING BOT            ║
║     Strategy: Edge scalping on 5m/15m prediction markets ║
║     Target: 2.5% profit / 1.0% stop → 2.5:1 R/R         ║
║     Break-even WR: ~30% | Capital: $100 | Position: $5   ║
║     Mode: Paper Trading (set PAPER_TRADING=false to live)║
╚══════════════════════════════════════════════════════════╝
"""

import asyncio
import json
import time
import os
import sys
import re
import argparse
import signal
from collections import deque
from datetime import datetime, timezone
from typing import Optional
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from dotenv import load_dotenv

# ── Hyperliquid API (CVD + liquidations + oracle price) ──
# Try local hyperliquid_api.py first, then fall back to polybot2 sibling dir.
HL_AVAILABLE = False
_hl_search = [
    os.path.dirname(os.path.abspath(__file__)),                          # local (same folder)
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "polybot2"),  # polybot2
]
for _d in _hl_search:
    try:
        if _d not in sys.path:
            sys.path.insert(0, _d)
        from hyperliquid_api import HyperliquidAPI as _HLApi
        HL_AVAILABLE = True
        break
    except ImportError:
        pass

# ── Try to import uvloop for faster event loop (Linux only) ──
try:
    import uvloop
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
    print("✅ uvloop enabled (faster event loop)")
except ImportError:
    print("⚠️  uvloop not installed. Run: pip install uvloop")

# ── External packages ──
try:
    import websockets
    import aiohttp
    from loguru import logger
except ImportError as e:
    print(f"❌ Missing package: {e}")
    print("Run: pip install websockets aiohttp loguru")
    sys.exit(1)

# ── Strip newlines from proxy env vars (httpx rejects them) ──
for _pkey in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    if _pkey in os.environ:
        os.environ[_pkey] = os.environ[_pkey].split("\n")[0].strip()

# ── Optional: Polymarket CLOB client ──
try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import (
        OrderArgs, BUY, SELL
    )
    CLOB_AVAILABLE = True
except ImportError:
    CLOB_AVAILABLE = False
    print("⚠️  py-clob-client not installed. Paper trading only.")
    print("   Run: pip install py-clob-client")

# ── Optional: Claude AI integration ──
try:
    import anthropic
    CLAUDE_AVAILABLE = True
except ImportError:
    CLAUDE_AVAILABLE = False

load_dotenv()

# ── BTC market discovery regex (flexible matching) ──
_BTC_SLUG_RE = re.compile(
    r"btc[-_]?(up[-_]?down|updown|higher|lower|prediction)", re.IGNORECASE
)
_BTC_TITLE_RE = re.compile(
    r"(?:btc|bitcoin).*?"
    r"(?:up\s*(?:or|/|,)\s*down|"       # "up or down", "up/down", "up, down"
    r"higher\s*or\s*lower|"              # "higher or lower"
    r"above\s*or\s*below|"              # "above or below"
    r"updown|"                           # "updown"
    r"(?:\d+)\s*[-\s]?(?:min|minute|m)\b)",  # "5 min", "15-minute", "5m"
    re.IGNORECASE
)
# Price-target BTC markets: "Will Bitcoin hit $90K?", "BTC above $85000", etc.
_BTC_PRICE_RE = re.compile(
    r"(?:btc|bitcoin).*?"
    r"(?:price|hit|reach|above|below|over|under)\s*"
    r"\$?\d",
    re.IGNORECASE
)

# ═══════════════════════════════════════════════════════════
#  CONFIGURATION
# ═══════════════════════════════════════════════════════════

@dataclass
class Config:
    # ── Polymarket Credentials ──
    api_key: str = field(default_factory=lambda: os.getenv("POLYMARKET_API_KEY", ""))
    api_secret: str = field(default_factory=lambda: os.getenv("POLYMARKET_API_SECRET", ""))
    api_passphrase: str = field(default_factory=lambda: os.getenv("POLYMARKET_API_PASSPHRASE", ""))
    private_key: str = field(default_factory=lambda: os.getenv("POLYMARKET_PRIVATE_KEY", ""))

    # ── Claude AI ──
    anthropic_key: str = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY", ""))
    claude_enabled: bool = field(default_factory=lambda: os.getenv("CLAUDE_ENABLED", "false").lower() == "true")

    # ── Trading Mode ──
    paper_trading: bool = field(default_factory=lambda: os.getenv("PAPER_TRADING", "true").lower() == "true")
    initial_capital: float = field(default_factory=lambda: float(os.getenv("INITIAL_CAPITAL", "1000")))

    # ── Risk Management ──
    max_trade_pct: float = 0.05          # 5% of capital per trade ($5 on $100)
    daily_loss_limit_pct: float = 0.10  # 10% daily loss limit ($10 on $100)
    min_edge_required: float = 0.005    # 0.5% minimum edge to enter
    min_profit_target: float = 0.025    # 2.5% profit target ($0.125 on $5)
    stop_loss_pct: float = 0.01         # 1.0% stop loss ($0.05 on $5) — R/R = 2.5:1
    max_hold_seconds: int = 270         # 4.5 minutes max hold
    consecutive_loss_limit: int = 5     # Circuit breaker: 5 losses → pause
    circuit_breaker_pause: int = 600    # Pause 10 minutes after circuit break
    wr_circuit_breaker_min: float = 0.30  # Pause if rolling win rate < 30%
    wr_circuit_breaker_window: int = 20   # Rolling window of last 20 trades
    wr_circuit_breaker_pause: int = 900   # Pause 15 minutes after WR breaker

    # ── Price-target market settings (aggressive profile) ──
    price_target_profit: float = 0.05    # 5% profit target (+$0.25 on $5)
    price_target_stop: float = 0.02      # 2% stop loss (-$0.10 on $5) → R/R = 2.5:1
    price_target_max_hold: int = 3600    # 1 hour max hold

    # ── Hold-to-expiry settings ──
    hold_to_expiry_threshold: float = 0.55  # Hold when price > 55% in our favor

    # ── Speed & Latency ──
    target_latency_ms: int = 100        # Target 100ms execution
    ws_reconnect_delay: float = 1.0

    # ── API Endpoints ──
    clob_ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    clob_rest_url: str = "https://clob.polymarket.com"
    gamma_api_url: str = "https://gamma-api.polymarket.com"
    # Kraken is accessible globally (no geo-block). Binance is blocked in some regions.
    # Kraken format: {"a":["price",...], "b":["price",...], ...} on the ticker channel
    binance_ws_url: str = "wss://stream.binance.com:9443/ws/btcusdt@ticker"
    kraken_ws_url: str = "wss://ws.kraken.com"
    hyperliquid_info_url: str = "https://api.hyperliquid.xyz/info"

    # ── Market Window Filter ──
    # 0 = trade all windows (5m + 15m), 300 = 5-min only, 900 = 15-min only
    market_window_seconds: int = field(
        default_factory=lambda: int(os.getenv("MARKET_WINDOW_SECONDS", "0"))
    )

    # ── Files ──
    log_dir: str = "logs"
    data_dir: str = "data"
    paper_trades_file: str = "data/paper_trades.json"


# ═══════════════════════════════════════════════════════════
#  DATA MODELS
# ═══════════════════════════════════════════════════════════

class Signal(Enum):
    YES = "YES"
    NO = "NO"
    HOLD = "HOLD"

class TradeStatus(Enum):
    OPEN = "open"
    CLOSED = "closed"
    CANCELLED = "cancelled"

@dataclass
class BTCPrice:
    price: float
    timestamp: float
    change_1m: float = 0.0
    change_5m: float = 0.0
    volume: float = 0.0

@dataclass
class MarketContract:
    condition_id: str
    question: str
    yes_price: float      # Current YES share price (0–1)
    no_price: float       # Current NO share price (0–1)
    expiry_ts: float      # Unix timestamp of market expiry
    start_ts: float = 0.0    # Market open timestamp (used to compute window)
    last_updated: float = field(default_factory=time.time)
    yes_token_id: str = ""   # CLOB token ID for YES side
    no_token_id: str  = ""   # CLOB token ID for NO side
    market_type: str = "updown"  # "updown" (5m/15m) or "price_target" (≤1hr)

    @property
    def seconds_to_expiry(self) -> float:
        return self.expiry_ts - time.time()

    @property
    def window_seconds(self) -> float:
        """Nominal duration of this market (e.g. 300 for 5-min, 900 for 15-min)."""
        if self.start_ts > 0:
            return self.expiry_ts - self.start_ts
        return 0.0

    @property
    def implied_prob_yes(self) -> float:
        return self.yes_price

@dataclass
class TradeRecord:
    trade_id: str
    condition_id: str
    side: str            # YES or NO
    entry_price: float
    shares: float
    capital_used: float
    entry_time: float
    token_id: str = ""   # Polygon token ID — required for live exit orders
    exit_price: float = 0.0
    exit_time: float = 0.0
    pnl: float = 0.0
    pnl_pct: float = 0.0
    status: str = "open"
    signal_strength: float = 0.0
    exit_reason: str = ""


# ═══════════════════════════════════════════════════════════
#  LOGGING SETUP
# ═══════════════════════════════════════════════════════════

def setup_logging(cfg: Config):
    Path(cfg.log_dir).mkdir(exist_ok=True)
    Path(cfg.data_dir).mkdir(exist_ok=True)

    logger.remove()
    # Console output
    logger.add(
        sys.stderr,
        format="<green>{time:HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | {message}",
        level="INFO",
        colorize=True
    )
    # File log
    logger.add(
        f"{cfg.log_dir}/bot.log",
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level} | {message}",
        level="DEBUG",
        rotation="100 MB",
        retention="7 days"
    )
    # Trade log (separate)
    logger.add(
        f"{cfg.log_dir}/trades.log",
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {message}",
        filter=lambda r: "TRADE" in r["message"],
        level="INFO"
    )


# ═══════════════════════════════════════════════════════════
#  CHAINLINK PRICE FEED (Polygon RPC — settlement source)
# ═══════════════════════════════════════════════════════════

class ChainlinkFeed:
    """
    Reads Chainlink BTC/USD directly from the Polygon aggregator contract.
    This is the EXACT price source Polymarket uses to resolve UP/DOWN markets.
    Updates every ~5s (Chainlink on Polygon updates when price moves >0.5%).
    """
    _RPC      = "https://1rpc.io/matic"
    _CONTRACT = "0xc907E116054Ad103354f2D350FD2514433D57F6f"
    _SELECTOR = "0x50d25bcd"  # latestAnswer()

    def __init__(self):
        self.price: float        = 0.0
        self.last_updated: float = 0.0
        self._running            = False

    async def start(self):
        self._running = True
        logger.info("📡 ChainlinkFeed starting (Polygon RPC — settlement price)...")
        async with aiohttp.ClientSession() as session:
            while self._running:
                try:
                    payload = {
                        "jsonrpc": "2.0", "method": "eth_call",
                        "params": [{"to": self._CONTRACT, "data": self._SELECTOR}, "latest"],
                        "id": 1,
                    }
                    async with session.post(
                        self._RPC, json=payload,
                        timeout=aiohttp.ClientTimeout(total=5)
                    ) as r:
                        data   = await r.json()
                        result = data.get("result", "")
                        if result and result != "0x" and "error" not in data:
                            new_price = int(result, 16) / 1e8
                            if new_price != self.price:
                                logger.debug(f"⛓  Chainlink BTC/USD updated: ${new_price:,.2f}")
                            self.price        = new_price
                            self.last_updated = time.time()
                except Exception as e:
                    logger.debug(f"Chainlink feed error: {e}")
                await asyncio.sleep(5)

    def stop(self):
        self._running = False


# ═══════════════════════════════════════════════════════════
#  HYPERLIQUID PRICE + SIGNAL FEED
# ═══════════════════════════════════════════════════════════

class HyperliquidPriceFeed:
    """
    Polls Hyperliquid oracle price, funding rate, and open interest every second.

    Oracle price = weighted median of Binance/Bybit/OKX spot prices.
    Used as the 3rd independent BTC price source (Binance → Kraken → Hyperliquid).
    Also provides funding rate and OI for signal enhancement.
    """

    _URL = "https://api.hyperliquid.xyz/info"

    def __init__(self):
        self.price: float = 0.0           # Oracle price (spot equivalent)
        self.mark_price: float = 0.0      # Perp mark price
        self.funding: float = 0.0         # Hourly funding rate (+ve = longs pay shorts)
        self.open_interest: float = 0.0   # OI in USD
        self.last_updated: float = 0.0
        self._running = False

    async def start(self):
        self._running = True
        logger.info("📡 HyperliquidFeed starting (oracle + funding + OI)...")
        async with aiohttp.ClientSession() as session:
            while self._running:
                try:
                    async with session.post(
                        self._URL,
                        json={"type": "metaAndAssetCtxs"},
                        timeout=aiohttp.ClientTimeout(total=5),
                    ) as r:
                        data = await r.json()
                        if isinstance(data, list) and len(data) == 2:
                            universe = data[0].get("universe", [])
                            ctx_list = data[1]
                            for i, asset in enumerate(universe):
                                if asset.get("name") == "BTC" and i < len(ctx_list):
                                    ctx     = ctx_list[i]
                                    oracle  = float(ctx.get("oraclePx",     0) or 0)
                                    mark    = float(ctx.get("markPx",       0) or 0)
                                    fund    = float(ctx.get("funding",      0) or 0)
                                    oi_c    = float(ctx.get("openInterest", 0) or 0)
                                    if oracle > 0:
                                        first = self.price == 0
                                        self.price         = oracle
                                        self.mark_price    = mark
                                        self.funding       = fund
                                        self.open_interest = oi_c * oracle
                                        self.last_updated  = time.time()
                                        if first:
                                            logger.success(
                                                f"✅ Hyperliquid oracle: ${oracle:,.2f} | "
                                                f"funding: {fund*100:.4f}%/hr | "
                                                f"OI: ${oi_c*oracle/1e9:.2f}B"
                                            )
                                    break
                except Exception as e:
                    logger.debug(f"HyperliquidFeed error: {e}")
                await asyncio.sleep(1)

    def stop(self):
        self._running = False


# ═══════════════════════════════════════════════════════════
#  BTC PRICE FEED (Binance WebSocket)
# ═══════════════════════════════════════════════════════════

class BTCPriceFeed:
    """
    Real-time BTC price feed — tries Binance first, falls back to Kraken.
    Binance is geo-blocked in some regions (HTTP 451). Kraken is globally accessible.
    Both feeds are free and require no authentication.
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.current: Optional[BTCPrice] = None
        self._price_history: deque[float] = deque(maxlen=300)
        self._running = False
        self._latency_ms: float = 0.0

    @property
    def latency_ms(self) -> float:
        return self._latency_ms

    async def connect(self):
        self._running = True
        # Try Binance first — if blocked/unreachable, switch to Kraken permanently.
        # _connect_binance returns True when it gives up on Binance.
        # _connect_kraken falls back to REST polling if WS is also blocked.
        blocked = await self._connect_binance()
        if blocked and self._running:
            logger.warning("⚠️  Switching BTC feed to Kraken (globally accessible)")
            await self._connect_kraken()

    async def _connect_binance(self) -> bool:
        """Connect to Binance. Returns True if blocked/unreachable (caller switches to Kraken)."""
        backoff = self.cfg.ws_reconnect_delay
        attempts = 0
        # Switch to Kraken after this many consecutive failures (handles 403, IP bans, proxies)
        MAX_ATTEMPTS = 4
        while self._running:
            try:
                logger.info("📡 Connecting to Binance BTC/USDT feed...")
                async with websockets.connect(
                    self.cfg.binance_ws_url,
                    ping_interval=20,
                    ping_timeout=10
                ) as ws:
                    logger.success("✅ Binance feed connected")
                    backoff = self.cfg.ws_reconnect_delay
                    attempts = 0
                    async for msg in ws:
                        t0 = time.perf_counter()
                        await self._handle_binance(msg)
                        self._latency_ms = (time.perf_counter() - t0) * 1000
            except websockets.exceptions.InvalidStatus as e:
                status = getattr(e.response, "status_code", None)
                # 451 = geo-block, 403 = IP/proxy block — both mean "try Kraken"
                if status in (403, 451):
                    logger.warning(f"⚠️  Binance blocked (HTTP {status}) — switching to Kraken")
                    return True
                attempts += 1
                logger.warning(f"Binance rejected (HTTP {status}) — retry {attempts} in {backoff:.0f}s")
                if attempts >= MAX_ATTEMPTS:
                    logger.warning(f"⚠️  Binance unreachable after {attempts} attempts — switching to Kraken")
                    return True
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
            except Exception as e:
                attempts += 1
                err = str(e)
                logger.warning(f"Binance feed error: {err} — retry {attempts} in {backoff:.0f}s")
                # Switch to Kraken on proxy rejection, connection refused, or DNS failure
                if any(x in err for x in ("403", "Forbidden", "refused", "Name or service not known",
                                           "Temporary failure", "host_not_allowed")):
                    logger.warning("⚠️  Binance unreachable (network/proxy) — switching to Kraken")
                    return True
                if attempts >= MAX_ATTEMPTS:
                    logger.warning(f"⚠️  Binance unreachable after {attempts} attempts — switching to Kraken")
                    return True
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
        return False

    async def _connect_kraken(self):
        """Connect to Kraken WebSocket — accessible globally, no geo-block.
        Falls back to REST polling if WebSocket is also blocked."""
        backoff = self.cfg.ws_reconnect_delay
        attempts = 0
        MAX_WS_ATTEMPTS = 4
        while self._running:
            try:
                logger.info("📡 Connecting to Kraken BTC/USD feed...")
                async with websockets.connect(self.cfg.kraken_ws_url, ping_interval=20) as ws:
                    # Subscribe to ticker channel
                    await ws.send(json.dumps({
                        "event": "subscribe",
                        "pair": ["XBT/USD"],
                        "subscription": {"name": "ticker"}
                    }))
                    logger.success("✅ Kraken feed connected")
                    backoff = self.cfg.ws_reconnect_delay
                    attempts = 0
                    async for msg in ws:
                        t0 = time.perf_counter()
                        await self._handle_kraken(msg)
                        self._latency_ms = (time.perf_counter() - t0) * 1000
            except Exception as e:
                attempts += 1
                logger.warning(f"Kraken feed error: {e} — reconnecting in {backoff:.0f}s ({attempts}/{MAX_WS_ATTEMPTS})")
                if attempts >= MAX_WS_ATTEMPTS:
                    logger.warning("⚠️  Kraken WS unreachable — falling back to REST price polling")
                    await self._poll_price_rest()
                    return
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _poll_price_rest(self):
        """Last-resort BTC price via Kraken REST API — no WebSocket needed."""
        url = "https://api.kraken.com/0/public/Ticker?pair=XBTUSD"
        backoff = 5.0
        logger.info("📡 Starting Kraken REST price polling (fallback mode)...")
        async with aiohttp.ClientSession() as session:
            while self._running:
                try:
                    t0 = time.perf_counter()
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                        data = await resp.json()
                        result = data.get("result", {})
                        ticker = result.get("XXBTZUSD") or result.get("XBTUSD", {})
                        price = float(ticker["c"][0]) if ticker.get("c") else 0.0
                        if price > 0:
                            self._latency_ms = (time.perf_counter() - t0) * 1000
                            first = self.current is None
                            self._update_price(price)
                            if first:
                                logger.info(f"💰 First BTC price from Kraken REST: ${price:,.2f}")
                        backoff = 5.0
                except Exception as e:
                    logger.warning(f"Kraken REST poll error: {e} — retry in {backoff:.0f}s")
                    backoff = min(backoff * 2, 60)
                await asyncio.sleep(backoff)

    async def _handle_binance(self, raw: str):
        try:
            data = json.loads(raw if isinstance(raw, str) else raw.decode())
            price = float(data.get("c", 0))  # 'c' = last price in Binance ticker
            if price <= 0:
                return
            first = self.current is None
            self._update_price(price, volume=float(data.get("v", 0)))
            if first:
                logger.info(f"💰 First BTC price from Binance: ${price:,.2f}")
        except Exception as e:
            logger.warning(f"Binance parse error: {e} | raw={str(raw)[:80]}")

    async def _handle_kraken(self, raw: str):
        try:
            data = json.loads(raw if isinstance(raw, str) else raw.decode())
            # Kraken ticker format: [channelID, {"c": ["price", "qty"], ...}, "ticker", "XBT/USD"]
            if not isinstance(data, list) or len(data) < 2:
                return
            ticker = data[1]
            if not isinstance(ticker, dict) or "c" not in ticker:
                return
            price = float(ticker["c"][0])
            if price <= 0:
                return
            first = self.current is None
            volume = float(ticker.get("v", [0, 0])[1])  # 24h volume
            self._update_price(price, volume=volume)
            if first:
                logger.info(f"💰 First BTC price from Kraken: ${price:,.2f}")
        except Exception as e:
            logger.warning(f"Kraken parse error: {e} | raw={str(raw)[:80]}")

    def _update_price(self, price: float, volume: float = 0.0):
        self._price_history.append(price)
        hist = list(self._price_history)
        change_1m = (price - hist[-60]) / hist[-60] if len(hist) >= 60 else 0.0
        change_5m = (price - hist[0]) / hist[0] if len(hist) >= 300 else 0.0
        self.current = BTCPrice(
            price=price,
            timestamp=time.time(),
            change_1m=change_1m,
            change_5m=change_5m,
            volume=volume
        )

    def stop(self):
        self._running = False


# ═══════════════════════════════════════════════════════════
#  POLYMARKET MARKET FEED (WebSocket + REST)
# ═══════════════════════════════════════════════════════════

class PolymarketFeed:
    """Fetches and tracks active BTC 5-min markets from Polymarket."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.markets: dict[str, MarketContract] = {}
        self._running = False
        self._session: Optional[aiohttp.ClientSession] = None

    async def start(self):
        self._session = aiohttp.ClientSession()
        self._running = True
        # Fetch markets every 30 seconds
        asyncio.create_task(self._poll_markets())
        # Subscribe to price updates via WebSocket
        asyncio.create_task(self._ws_prices())

    async def _poll_markets(self):
        """Discover active BTC 5m/15m markets via events endpoint."""
        while self._running:
            try:
                url    = f"{self.cfg.gamma_api_url}/events"
                params = {
                    "active":    "true",
                    "closed":    "false",
                    "limit":     200,
                    "order":     "endDate",
                    "ascending": "true",
                }
                async with self._session.get(url, params=params) as resp:
                    if resp.status == 200:
                        events = await resp.json()
                        await self._process_events(events)
                    else:
                        logger.warning(f"Gamma API returned HTTP {resp.status}")
            except Exception as e:
                logger.warning(f"Market poll error: {e}")
            await asyncio.sleep(30)

    async def _process_events(self, events):
        """Extract BTC up/down + price-target markets from events."""
        new_count       = 0
        now             = time.time()
        searched        = 0
        matched         = 0
        rejected_closed = 0
        rejected_expired = 0
        rejected_long   = 0

        for event in events:
            searched += 1
            slug  = (event.get("slug", "") or event.get("ticker", "")).lower()
            title = (event.get("title", "") or event.get("question", "")).lower()

            # Accept if slug, title, OR price-target pattern matches
            slug_match  = bool(_BTC_SLUG_RE.search(slug))
            title_match = bool(_BTC_TITLE_RE.search(title))
            price_match = bool(_BTC_PRICE_RE.search(title))
            if not (slug_match or title_match or price_match):
                continue
            matched += 1
            if event.get("closed") or not event.get("active"):
                rejected_closed += 1
                continue

            for m in event.get("markets", []):
                cid = m.get("conditionId", "")
                if not cid:
                    continue

                # Parse expiry — skip already closed
                try:
                    expiry_ts = datetime.fromisoformat(
                        m.get("endDate", "").replace("Z", "+00:00")
                    ).timestamp()
                except Exception:
                    expiry_ts = now + 300
                if expiry_ts <= now:
                    rejected_expired += 1
                    continue

                # Parse start timestamp (used to detect 5-min vs 15-min window)
                try:
                    start_ts = datetime.fromisoformat(
                        m.get("startDate", "").replace("Z", "+00:00")
                    ).timestamp()
                except Exception:
                    start_ts = expiry_ts - 300  # Assume 5-min if unknown

                # Determine market type and max allowed duration
                is_price_target = price_match and not (slug_match or title_match)
                max_duration = 3600 if is_price_target else 1800  # 1hr for price targets, 30min for up/down
                duration = expiry_ts - start_ts
                if duration > max_duration:
                    rejected_long += 1
                    logger.debug(f"  Skipping long market ({int(duration)}s): {m.get('question','?')[:60]}")
                    continue

                # YES/NO prices
                try:
                    prices = json.loads(m.get("outcomePrices", "[0.5,0.5]"))
                except Exception:
                    prices = [0.5, 0.5]
                yes_price = float(prices[0]) if prices else 0.5
                no_price  = float(prices[1]) if len(prices) > 1 else round(1 - yes_price, 4)

                # Token IDs for live order placement
                try:
                    token_ids = json.loads(m.get("clobTokenIds", "[]"))
                except Exception:
                    token_ids = []
                yes_token = str(token_ids[0]) if len(token_ids) > 0 else ""
                no_token  = str(token_ids[1]) if len(token_ids) > 1 else ""

                contract = MarketContract(
                    condition_id=cid,
                    question=m.get("question", ""),
                    yes_price=yes_price,
                    no_price=no_price,
                    expiry_ts=expiry_ts,
                    start_ts=start_ts,
                    yes_token_id=yes_token,
                    no_token_id=no_token,
                    market_type="price_target" if is_price_target else "updown",
                )

                if cid not in self.markets:
                    new_count += 1
                    win = int(expiry_ts - start_ts)
                    logger.debug(f"  New market: {m.get('question','?')[:60]} | window={win}s")
                self.markets[cid] = contract

        # Diagnostic logging — always log scan stats so user can see what happened
        logger.info(
            f"📡 Market scan: {searched} events | {matched} BTC matches | "
            f"{rejected_closed} closed | {rejected_expired} expired | "
            f"{rejected_long} too long | "
            f"{len(self.markets)} tracked ({new_count} new)"
        )
        # Enhanced diagnostics when no markets found
        if len(self.markets) == 0 and matched > 0:
            logger.warning(
                f"⚠️  {matched} BTC events found but ALL filtered out: "
                f"{rejected_closed} closed, {rejected_expired} expired, "
                f"{rejected_long} too long"
            )
        elif matched == 0:
            logger.warning(
                f"⚠️  No BTC markets in {searched} events. "
                f"Polymarket may not have active BTC prediction markets right now."
            )

    async def _ws_prices(self):
        """Subscribe to real-time price updates via Polymarket WebSocket."""
        backoff = self.cfg.ws_reconnect_delay
        while self._running:
            try:
                headers = {
                    "Origin": "https://polymarket.com",
                    "User-Agent": "Mozilla/5.0 (compatible; PolyBot/1.0)",
                }
                async with websockets.connect(
                    self.cfg.clob_ws_url,
                    additional_headers=headers,
                    ping_interval=30
                ) as ws:
                    logger.success("✅ Polymarket price feed connected")
                    backoff = self.cfg.ws_reconnect_delay  # reset on success

                    for cid in list(self.markets.keys()):
                        sub_msg = json.dumps({
                            "type": "subscribe",
                            "channel": "book",
                            "market": cid
                        })
                        await ws.send(sub_msg)

                    async for msg in ws:
                        await self._handle_price_update(msg)

            except websockets.exceptions.InvalidStatus as e:
                status = getattr(e.response, "status_code", None)
                # Permanent errors — stop retrying, REST polling will continue
                if status == 451:
                    logger.error(
                        "❌ Polymarket WS blocked (HTTP 451 — geo-restriction). "
                        "Use a VPN. Falling back to REST polling (30s updates)."
                    )
                    return
                if status == 404:
                    logger.error(
                        "❌ Polymarket WS endpoint not found (HTTP 404). "
                        "The WebSocket URL may have changed. "
                        "Falling back to REST polling (30s updates) — bot continues normally."
                    )
                    return
                if status in (403, 410):
                    logger.error(f"❌ Polymarket WS permanent error (HTTP {status}) — falling back to REST polling.")
                    return
                # Transient errors — retry with backoff
                logger.warning(f"Polymarket WS rejected (HTTP {status}) — retrying in {backoff:.0f}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
            except Exception as e:
                logger.warning(f"Polymarket WS error: {e} — retrying in {backoff:.0f}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _handle_price_update(self, raw: str):
        try:
            data = json.loads(raw)
            if data.get("type") == "book":
                cid = data.get("market_id", "")
                if cid in self.markets:
                    # Update YES/NO prices from order book data
                    bids = data.get("bids", [])
                    asks = data.get("asks", [])
                    if bids and asks:
                        best_bid = float(bids[0][0]) if bids else self.markets[cid].yes_price
                        best_ask = float(asks[0][0]) if asks else self.markets[cid].yes_price
                        mid = (best_bid + best_ask) / 2
                        self.markets[cid].yes_price = mid
                        self.markets[cid].no_price = 1.0 - mid
                        self.markets[cid].last_updated = time.time()
        except Exception:
            pass

    def get_active_markets(self) -> list[MarketContract]:
        """
        Return markets that haven't expired and are still tradeable.
        If cfg.market_window_seconds > 0, only returns markets matching that window:
          300 → 5-min markets only
          900 → 15-min markets only
          0   → all windows (default)
        """
        result = []
        for m in self.markets.values():
            if not (30 < m.seconds_to_expiry < 1200):
                continue
            # Window filter (optional)
            win_filter = self.cfg.market_window_seconds
            if win_filter > 0 and m.window_seconds > 0:
                if abs(m.window_seconds - win_filter) > 120:   # ±2 min tolerance
                    continue
            result.append(m)
        return result

    async def stop(self):
        self._running = False
        if self._session:
            await self._session.close()


# ═══════════════════════════════════════════════════════════
#  ENHANCED SIGNAL FEED — CVD + Liquidations from polybot2
# ═══════════════════════════════════════════════════════════

class EnhancedSignalFeed:
    """
    Background task that polls Hyperliquid CVD and exchange liquidations.
    Produces two floats updated every ~60s:
      cvd_signal  : -1.0 (bearish div) … +1.0 (bullish div)
      liq_signal  : -1.0 (long liqs dominate) … +1.0 (short liqs dominate)
    """

    def __init__(self):
        self.cvd_signal: float     = 0.0
        self.liq_signal: float     = 0.0
        self.funding_signal: float = 0.0   # -1 (overbought) … +1 (oversold)
        self.oi_signal: float      = 0.0   # -1 (OI collapsing) … +1 (OI growing)
        self._prev_oi: float       = 0.0   # For OI delta calculation
        self._running = False
        self._api = _HLApi() if HL_AVAILABLE else None
        if not HL_AVAILABLE:
            logger.warning("⚠️  hyperliquid_api not found — running without CVD/liq/funding signals")
            logger.warning("    Create hyperliquid_api.py in the same folder as bot.py")

    async def start(self):
        if not self._api:
            return
        self._running = True
        logger.info("📡 EnhancedSignalFeed started (CVD + liquidations)")
        while self._running:
            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, self._refresh)
            except Exception as e:
                logger.debug(f"EnhancedSignalFeed error: {e}")
            await asyncio.sleep(60)

    def _refresh(self):
        self._update_cvd()
        self._update_liquidations()
        self._update_funding_oi()

    def _update_cvd(self):
        """
        Compute CVD over the last 15 1-min candles.
        Bearish divergence (price up, CVD down) → -1.0
        Bullish divergence (price down, CVD up) → +1.0
        Aligned trend                            → ±0.3
        No signal                                →  0.0
        """
        try:
            candles = self._api.get_candles("BTC", "1m", lookback_hours=1)
            if not candles or len(candles) < 15:
                return
            candles = candles[-15:]

            prices  = [float(c.get("c", 0)) for c in candles]
            volumes = [float(c.get("v", 0)) for c in candles]
            opens   = [float(c.get("o", 0)) for c in candles]

            # CVD: green bar = +vol, red bar = -vol
            deltas = [v if c >= o else -v for c, o, v in zip(prices, opens, volumes)]
            cvd_chg   = sum(deltas)
            price_chg = (prices[-1] - prices[0]) / prices[0] * 100 if prices[0] else 0

            if price_chg > 0.05 and cvd_chg < -50:
                self.cvd_signal = -1.0   # bearish divergence
            elif price_chg < -0.05 and cvd_chg > 50:
                self.cvd_signal = 1.0    # bullish divergence
            elif price_chg > 0.05 and cvd_chg > 50:
                self.cvd_signal = 0.3    # aligned bull
            elif price_chg < -0.05 and cvd_chg < -50:
                self.cvd_signal = -0.3   # aligned bear
            else:
                self.cvd_signal = 0.0

            logger.debug(f"CVD signal={self.cvd_signal:+.1f} price_chg={price_chg:+.3f}% cvd_chg={cvd_chg:+.0f}")
        except Exception as e:
            logger.debug(f"CVD update error: {e}")

    def _update_liquidations(self):
        """
        Blended liquidation signal from two sources:
        1. Binance/OKX executed liquidations (what already happened)
        2. Hyperliquid near-liquidation pressure (what's about to happen)

        More short liqs (forced short covering) → bullish  → +1.0
        More long liqs  (forced long selling)   → bearish  → -1.0
        """
        try:
            # Source 1: Executed liquidations from Binance + OKX
            exchange_signal = 0.0
            liqs = self._api.get_all_liquidations("1h")
            if liqs:
                long_usd  = sum(l["value"] for l in liqs if l.get("side") == "long")
                short_usd = sum(l["value"] for l in liqs if l.get("side") == "short")
                total     = long_usd + short_usd
                if total > 0:
                    exchange_signal = (short_usd - long_usd) / total

            # Source 2: Near-liquidation pressure from Hyperliquid
            hl_pressure = 0.0
            try:
                lp = self._api.get_liquidation_pressure("BTC")
                hl_pressure = lp.get("liq_pressure", 0.0)
            except Exception:
                pass

            # Blend: 60% near-liquidation pressure + 40% executed liquidations
            self.liq_signal = 0.6 * hl_pressure + 0.4 * exchange_signal
            logger.debug(
                f"Liq signal={self.liq_signal:+.2f} "
                f"(HL pressure={hl_pressure:+.2f}, exchange={exchange_signal:+.2f})"
            )
        except Exception as e:
            logger.debug(f"Liq update error: {e}")

    def _update_funding_oi(self):
        """
        Funding rate signal:
          High positive funding (>0.02%/hr) → longs are crowded → bearish pressure → -0.5
          High negative funding (<-0.02%/hr) → shorts are crowded → bullish squeeze → +0.5
          Near zero → neutral (0.0)

        OI delta signal:
          OI growing fast (+2%) → new money entering, trend is real → reinforce momentum
          OI shrinking fast (-2%) → positions closing, trend losing steam → fade signal
        """
        try:
            fi = self._api.get_funding_and_oi("BTC")
            funding = fi.get("funding", 0.0)
            oi      = fi.get("open_interest", 0.0)

            # Funding signal — thresholds in %/hr
            # Typical HL funding: -0.01% to +0.01% per hour
            if   funding >  0.0002:  self.funding_signal = -0.5   # Very overbought
            elif funding >  0.00005: self.funding_signal = -0.2   # Mildly overbought
            elif funding < -0.0002:  self.funding_signal =  0.5   # Very oversold
            elif funding < -0.00005: self.funding_signal =  0.2   # Mildly oversold
            else:                    self.funding_signal =  0.0

            # OI delta signal (compare to previous reading)
            if self._prev_oi > 0 and oi > 0:
                oi_delta = (oi - self._prev_oi) / self._prev_oi
                if   oi_delta >  0.02:  self.oi_signal =  0.3   # OI growing → trend real
                elif oi_delta < -0.02:  self.oi_signal = -0.3   # OI shrinking → fade
                else:                   self.oi_signal =  0.0
            if oi > 0:
                self._prev_oi = oi

            logger.debug(
                f"Funding signal={self.funding_signal:+.1f} ({funding*100:.4f}%/hr) | "
                f"OI signal={self.oi_signal:+.1f} OI=${oi/1e9:.2f}B"
            )
        except Exception as e:
            logger.debug(f"Funding/OI update error: {e}")

    def stop(self):
        self._running = False


# ═══════════════════════════════════════════════════════════
#  SIGNAL ENGINE — Core Strategy Logic
# ═══════════════════════════════════════════════════════════

class SignalEngine:
    """
    Strategy: Detect when BTC price momentum diverges from Polymarket
    contract probability by >0.3%. Enter position, exit at target.

    Signal Logic:
    - If BTC is strongly trending UP and YES price < 0.53 → BUY YES
    - If BTC is strongly trending DOWN and NO price < 0.53 → BUY NO
    - Edge = |implied_probability - fair_value| — must be > min_edge
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg

    def calculate_fair_value(
        self,
        btc: BTCPrice,
        cvd_signal: float = 0.0,
        liq_signal: float = 0.0,
        funding_signal: float = 0.0,
        oi_signal: float = 0.0,
        chainlink_price: float = 0.0,
    ) -> float:
        """
        Estimate fair probability that BTC will be UP at expiry.

        Uses Chainlink as the anchor (settlement price) and Binance momentum
        to predict which direction Chainlink will move next.

        Signal weights (must sum to ~1.0 across all adjustments):
          momentum  — Chainlink deviation + 1m/5m Binance change  (dominant)
          cvd       — cumulative volume delta divergence           (8%)
          liq       — liquidation pressure (long vs short liqs)   (6%)
          funding   — funding rate crowding signal                 (5%)
          oi        — open interest delta (trend conviction)       (4%)
        """
        # ── Use Chainlink as anchor if available, else fall back to Binance ──
        anchor = chainlink_price if chainlink_price > 0 else btc.price

        # Momentum: how far has Binance moved vs Chainlink's last reading?
        # Positive = Binance is above Chainlink → next CL update likely UP
        if anchor > 0:
            cl_deviation = (btc.price - anchor) / anchor  # e.g. +0.003 = Binance 0.3% above CL
        else:
            cl_deviation = 0.0

        adj_1m       = min(max(btc.change_1m * 10,   -0.15), 0.15)
        adj_5m       = min(max(btc.change_5m * 5,    -0.10), 0.10)
        adj_cl_dev   = min(max(cl_deviation  * 15,   -0.12), 0.12)  # strongest signal
        momentum_adj = (adj_cl_dev * 0.5 + adj_1m * 0.35 + adj_5m * 0.15)

        cvd_adj     = cvd_signal     * 0.07   # Cumulative volume delta
        liq_adj     = liq_signal     * 0.10   # Near-liquidation pressure (increased from 0.06)
        funding_adj = funding_signal * 0.05   # High positive funding → bearish pressure
        oi_adj      = oi_signal      * 0.03   # Growing OI → trend has conviction

        fair_value = 0.50 + momentum_adj + cvd_adj + liq_adj + funding_adj + oi_adj
        return min(max(fair_value, 0.02), 0.98)

    def generate_signal(
        self,
        contract: MarketContract,
        btc: BTCPrice,
        cvd_signal: float = 0.0,
        liq_signal: float = 0.0,
        funding_signal: float = 0.0,
        oi_signal: float = 0.0,
        chainlink_price: float = 0.0,
    ) -> tuple[Signal, float, float]:
        """
        Returns: (signal, edge, confidence)
        signal: YES, NO, or HOLD
        edge: estimated profit margin
        confidence: 0.0–1.0
        """
        if contract.seconds_to_expiry < 30:
            return Signal.HOLD, 0.0, 0.0

        fair_value = self.calculate_fair_value(
            btc, cvd_signal, liq_signal, funding_signal, oi_signal, chainlink_price
        )

        yes_edge = fair_value - contract.yes_price
        no_edge = (1 - fair_value) - contract.no_price

        best_edge = max(yes_edge, no_edge)

        if best_edge < self.cfg.min_edge_required:
            return Signal.HOLD, best_edge, 0.0

        # Determine signal direction
        if yes_edge > no_edge and yes_edge >= self.cfg.min_edge_required:
            signal = Signal.YES
            edge = yes_edge
        elif no_edge > yes_edge and no_edge >= self.cfg.min_edge_required:
            signal = Signal.NO
            edge = no_edge
        else:
            return Signal.HOLD, 0.0, 0.0

        # Confidence score — weighted by edge magnitude and momentum strength
        momentum_strength = abs(btc.change_1m) / 0.005  # Normalize to 0.5% ref
        confidence = min(edge * 100 + momentum_strength * 0.2, 1.0)

        return signal, edge, confidence


# ═══════════════════════════════════════════════════════════
#  RISK MANAGER
# ═══════════════════════════════════════════════════════════

class RiskManager:
    def __init__(self, cfg: Config, capital: float):
        self.cfg = cfg
        self.capital = capital
        self.daily_start_capital = capital
        self.consecutive_losses = 0
        self.circuit_breaker_until: float = 0.0
        self.trades_today = 0
        self.wins_today = 0
        self.losses_today = 0
        # Rolling win-rate circuit breaker
        self._recent_results: list[bool] = []  # last N trade results (True=win)
        self._wr_pause_until: float = 0.0

    @property
    def daily_pnl_pct(self) -> float:
        return (self.capital - self.daily_start_capital) / self.daily_start_capital

    @property
    def is_circuit_breaker_active(self) -> bool:
        return time.time() < self.circuit_breaker_until

    @property
    def rolling_win_rate(self) -> float:
        """Win rate over the last N trades (rolling window)."""
        if not self._recent_results:
            return 0.0
        return sum(self._recent_results) / len(self._recent_results)

    def can_trade(self) -> tuple[bool, str]:
        """Returns (can_trade, reason)"""
        if self.is_circuit_breaker_active:
            remaining = int(self.circuit_breaker_until - time.time())
            return False, f"Circuit breaker active ({remaining}s remaining)"

        # Win-rate circuit breaker: pause if rolling WR < 30% over last 20 trades
        if time.time() < self._wr_pause_until:
            remaining = int(self._wr_pause_until - time.time())
            return False, f"Win rate breaker active ({remaining}s remaining, WR was <{self.cfg.wr_circuit_breaker_min*100:.0f}%)"

        if self.daily_pnl_pct <= -self.cfg.daily_loss_limit_pct:
            return False, f"Daily loss limit hit ({self.daily_pnl_pct*100:.2f}%)"

        return True, "OK"

    def calculate_position_size(self, confidence: float) -> float:
        """
        Position size = capital × max_trade_pct × confidence_multiplier
        Sideways market (low confidence) → smaller positions
        Trending market (high confidence) → larger positions
        """
        base_size = self.capital * self.cfg.max_trade_pct
        confidence_multiplier = 0.5 + (confidence * 0.5)  # 0.5 to 1.0
        size = base_size * confidence_multiplier
        return round(min(size, self.capital * self.cfg.max_trade_pct), 2)

    def record_trade_result(self, pnl: float):
        self.capital += pnl
        self.trades_today += 1

        # Track rolling win rate
        self._recent_results.append(pnl > 0)
        if len(self._recent_results) > self.cfg.wr_circuit_breaker_window:
            self._recent_results.pop(0)

        if pnl > 0:
            self.wins_today += 1
            self.consecutive_losses = 0
        else:
            self.losses_today += 1
            self.consecutive_losses += 1
            if self.consecutive_losses >= self.cfg.consecutive_loss_limit:
                self.circuit_breaker_until = time.time() + self.cfg.circuit_breaker_pause
                logger.warning(f"⚡ CIRCUIT BREAKER: {self.cfg.consecutive_loss_limit} consecutive losses — pausing {self.cfg.circuit_breaker_pause}s")

        # Check rolling win rate after enough trades
        if len(self._recent_results) >= self.cfg.wr_circuit_breaker_window:
            if self.rolling_win_rate < self.cfg.wr_circuit_breaker_min:
                self._wr_pause_until = time.time() + self.cfg.wr_circuit_breaker_pause
                logger.warning(
                    f"⚡ WIN RATE BREAKER: {self.rolling_win_rate*100:.0f}% < "
                    f"{self.cfg.wr_circuit_breaker_min*100:.0f}% over last "
                    f"{self.cfg.wr_circuit_breaker_window} trades — pausing "
                    f"{self.cfg.wr_circuit_breaker_pause}s"
                )
                self._recent_results.clear()  # Reset after pause

    @property
    def win_rate(self) -> float:
        if self.trades_today == 0:
            return 0.0
        return self.wins_today / self.trades_today

    def report(self) -> str:
        rolling = f" | Rolling WR({len(self._recent_results)}): {self.rolling_win_rate*100:.1f}%" if self._recent_results else ""
        return (
            f"Capital: ${self.capital:.2f} | "
            f"Daily P&L: {self.daily_pnl_pct*100:+.2f}% | "
            f"Trades: {self.trades_today} | "
            f"Win Rate: {self.win_rate*100:.1f}%{rolling} | "
            f"Consecutive Losses: {self.consecutive_losses}"
        )


# ═══════════════════════════════════════════════════════════
#  PAPER TRADER (Simulates fills with real prices)
# ═══════════════════════════════════════════════════════════

class PaperTrader:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.open_trades: dict[str, TradeRecord] = {}
        self.closed_trades: list[TradeRecord] = []
        self._trade_counter = 0
        self._load_history()

    def _load_history(self):
        try:
            if os.path.exists(self.cfg.paper_trades_file):
                with open(self.cfg.paper_trades_file) as f:
                    data = json.load(f)
                    self.closed_trades = [TradeRecord(**t) for t in data.get("closed", [])]
                    logger.info(f"📂 Loaded {len(self.closed_trades)} historical paper trades")
        except Exception as e:
            logger.warning(f"Could not load trade history: {e}")

    def _save_history(self):
        try:
            Path(self.cfg.data_dir).mkdir(exist_ok=True)
            all_trades = self.closed_trades[-500:]  # Keep last 500
            with open(self.cfg.paper_trades_file, "w") as f:
                json.dump({"closed": [asdict(t) for t in all_trades]}, f, indent=2)
        except Exception as e:
            logger.warning(f"Could not save trades: {e}")

    async def enter(
        self,
        contract: MarketContract,
        signal: Signal,
        shares: float,
        risk: RiskManager
    ) -> Optional[TradeRecord]:
        """Simulate order entry at current market price."""
        t0 = time.perf_counter()

        # Simulate 50ms execution delay (paper mode)
        await asyncio.sleep(0.05)

        entry_price = contract.yes_price if signal == Signal.YES else contract.no_price
        capital_used = shares * entry_price

        self._trade_counter += 1
        trade = TradeRecord(
            trade_id=f"PAPER-{self._trade_counter:05d}",
            condition_id=contract.condition_id,
            side=signal.value,
            entry_price=entry_price,
            shares=shares,
            capital_used=capital_used,
            entry_time=time.time()
        )

        self.open_trades[trade.trade_id] = trade
        latency_ms = (time.perf_counter() - t0) * 1000

        logger.info(
            f"📈 TRADE ENTER | {trade.trade_id} | "
            f"BUY {signal.value} @ ${entry_price:.4f} | "
            f"{shares:.1f} shares | ${capital_used:.2f} | "
            f"{latency_ms:.1f}ms"
        )
        return trade

    async def exit(
        self,
        trade: TradeRecord,
        exit_price: float,
        risk: RiskManager,
        reason: str = "target"
    ) -> TradeRecord:
        """Simulate order exit and calculate P&L."""
        # Paper mode: simulate exit at target price
        await asyncio.sleep(0.05)

        trade.exit_price = exit_price
        trade.exit_time = time.time()
        trade.pnl = (exit_price - trade.entry_price) * trade.shares
        trade.pnl_pct = (exit_price - trade.entry_price) / trade.entry_price
        trade.status = TradeStatus.CLOSED.value
        trade.exit_reason = reason

        risk.record_trade_result(trade.pnl)

        if trade.trade_id in self.open_trades:
            del self.open_trades[trade.trade_id]
        self.closed_trades.append(trade)
        self._save_history()

        icon = "✅" if trade.pnl > 0 else "❌"
        logger.info(
            f"{icon} TRADE EXIT | {trade.trade_id} | "
            f"SELL {trade.side} @ ${exit_price:.4f} | "
            f"P&L: ${trade.pnl:+.4f} ({trade.pnl_pct*100:+.2f}%) | "
            f"Reason: {reason}"
        )
        logger.info(f"   💰 {risk.report()}")
        return trade

    def summary(self) -> dict:
        if not self.closed_trades:
            return {"total_trades": 0}
        pnls = [t.pnl for t in self.closed_trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        return {
            "total_trades": len(self.closed_trades),
            "win_rate": len(wins) / len(self.closed_trades) if pnls else 0,
            "total_pnl": sum(pnls),
            "avg_win": sum(wins) / len(wins) if wins else 0,
            "avg_loss": sum(losses) / len(losses) if losses else 0,
            "best_trade": max(pnls) if pnls else 0,
            "worst_trade": min(pnls) if pnls else 0,
        }


# ═══════════════════════════════════════════════════════════
#  LIVE TRADER (Real orders via CLOB)
# ═══════════════════════════════════════════════════════════

class LiveTrader:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.client: Optional[ClobClient] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._initialize_client()

    def _initialize_client(self):
        if not CLOB_AVAILABLE:
            logger.error("❌ py-clob-client not installed — cannot do live trading")
            return
        if not self.cfg.private_key:
            logger.error("❌ POLYMARKET_PRIVATE_KEY not set in .env")
            return
        try:
            self.client = ClobClient(
                host=self.cfg.clob_rest_url,
                chain_id=137,  # Polygon Mainnet
                key=self.cfg.private_key,
                creds={
                    "apiKey": self.cfg.api_key,
                    "secret": self.cfg.api_secret,
                    "passphrase": self.cfg.api_passphrase
                }
            )
            logger.success("✅ Polymarket CLOB client initialized")
        except Exception as e:
            logger.error(f"CLOB client init failed: {e}")
            self.client = None

    async def _ensure_session(self):
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession()

    async def enter(
        self,
        contract: MarketContract,
        signal: Signal,
        shares: float,
        risk: RiskManager
    ) -> Optional[TradeRecord]:
        if not self.client:
            logger.error("CLOB client not available")
            return None

        t0 = time.perf_counter()
        try:
            await self._ensure_session()
            token_id = await self._get_token_id(contract, signal)
            if not token_id:
                return None

            entry_price = contract.yes_price if signal == Signal.YES else contract.no_price

            order = OrderArgs(
                token_id=token_id,
                price=entry_price,
                size=shares,
                side=BUY
            )
            result = self.client.create_and_post_order(order)
            latency_ms = (time.perf_counter() - t0) * 1000

            logger.info(
                f"🟢 LIVE ORDER | BUY {signal.value} @ ${entry_price:.4f} | "
                f"{shares} shares | {latency_ms:.1f}ms | OrderID: {result.get('orderID','?')}"
            )

            trade = TradeRecord(
                trade_id=result.get("orderID", f"LIVE-{int(time.time())}"),
                condition_id=contract.condition_id,
                side=signal.value,
                entry_price=entry_price,
                shares=shares,
                capital_used=shares * entry_price,
                entry_time=time.time(),
                token_id=token_id,
            )
            return trade

        except Exception as e:
            logger.error(f"Order entry failed: {e}")
            return None

    async def _get_token_id(self, contract: MarketContract, signal: Signal) -> Optional[str]:
        """Fetch YES/NO token ID for a market using the shared session."""
        try:
            url = f"{self.cfg.gamma_api_url}/markets"
            async with self._session.get(url, params={"conditionId": contract.condition_id}) as r:
                data = await r.json()
                for m in data:
                    for token in m.get("tokens", []):
                        if token.get("outcome", "").upper() == signal.value:
                            return token.get("token_id")
        except Exception as e:
            logger.error(f"Token ID fetch failed: {e}")
        return None

    async def exit(
        self,
        trade: TradeRecord,
        exit_price: float,
        risk: RiskManager,
        reason: str = "target"
    ) -> TradeRecord:
        if not self.client:
            return trade
        try:
            if not trade.token_id:
                logger.error(f"Cannot exit {trade.trade_id} — token_id missing")
                return trade

            order = OrderArgs(
                token_id=trade.token_id,
                price=exit_price,
                size=trade.shares,
                side=SELL
            )
            result = self.client.create_and_post_order(order)

            trade.exit_price = exit_price
            trade.exit_time = time.time()
            trade.pnl = (exit_price - trade.entry_price) * trade.shares
            trade.pnl_pct = (exit_price - trade.entry_price) / trade.entry_price
            trade.status = TradeStatus.CLOSED.value
            trade.exit_reason = reason
            risk.record_trade_result(trade.pnl)

            logger.info(
                f"🔴 LIVE SELL | {trade.trade_id} @ ${exit_price:.4f} | "
                f"P&L: ${trade.pnl:+.4f} | Reason: {reason} | "
                f"OrderID: {result.get('orderID','?')}"
            )
        except Exception as e:
            logger.error(f"Order exit failed: {e}")
        return trade

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()


# ═══════════════════════════════════════════════════════════
#  CLAUDEBOT — AI-Powered Signal Enhancement
# ═══════════════════════════════════════════════════════════

class ClaudeBot:
    """
    Uses Claude AI to analyze market regime and enhance signal quality.
    Classifies market as: trending_up, trending_down, sideways, volatile
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.client = None
        self._last_analysis: str = "unknown"
        self._last_analysis_time: float = 0
        self._cache_seconds: int = 60  # Re-analyze every 60 seconds

        if CLAUDE_AVAILABLE and cfg.anthropic_key and cfg.claude_enabled:
            self.client = anthropic.Anthropic(api_key=cfg.anthropic_key)
            logger.success("✅ ClaudeBot AI integration enabled")
        elif cfg.claude_enabled:
            logger.warning("⚠️  Claude AI requested but anthropic package missing or key not set")

    async def analyze_market_regime(self, btc: BTCPrice) -> dict:
        """
        Ask Claude to classify the current market regime.
        Returns regime classification and confidence multiplier.
        """
        if not self.client:
            return {"regime": "unknown", "trade_multiplier": 1.0, "reasoning": "Claude not available"}

        # Use cache to avoid too many API calls
        if time.time() - self._last_analysis_time < self._cache_seconds:
            return {"regime": self._last_analysis, "trade_multiplier": 1.0, "reasoning": "cached"}

        try:
            prompt = f"""You are a crypto market microstructure analyst for a high-frequency trading bot.

Current BTC market data:
- Price: ${btc.price:,.2f}
- 1-minute change: {btc.change_1m*100:+.4f}%
- 5-minute change: {btc.change_5m*100:+.4f}%
- Volume (24h): {btc.volume:,.0f}
- Timestamp: {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}

Task: Classify the current micro-trend for the next 5 minutes and provide a trading multiplier.

Respond ONLY with valid JSON in this exact format:
{{
  "regime": "trending_up|trending_down|sideways|volatile",
  "confidence": 0.0-1.0,
  "trade_multiplier": 0.5-1.5,
  "bias": "YES|NO|NEUTRAL",
  "reasoning": "one sentence max"
}}

Rules:
- trending_up/down: clear directional move, multiplier 1.2-1.5
- sideways: no clear direction, multiplier 0.5-0.8
- volatile: erratic moves, multiplier 0.6-0.9
- trade_multiplier affects position sizing: >1.0 means increase size"""

            response = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: self.client.messages.create(
                    model="claude-sonnet-4-6",
                    max_tokens=200,
                    messages=[{"role": "user", "content": prompt}]
                )
            )

            raw = response.content[0].text.strip()
            # Strip markdown fences if present
            raw = raw.replace("```json", "").replace("```", "").strip()
            result = json.loads(raw)

            self._last_analysis = result.get("regime", "unknown")
            self._last_analysis_time = time.time()

            logger.debug(f"🤖 ClaudeBot: {result}")
            return result

        except Exception as e:
            logger.debug(f"ClaudeBot error: {e}")
            return {"regime": "unknown", "trade_multiplier": 1.0, "reasoning": str(e)}


# ═══════════════════════════════════════════════════════════
#  POSITION MONITOR — Auto-exit logic
# ═══════════════════════════════════════════════════════════

class PositionMonitor:
    """
    Monitors open positions and triggers exits when:
    1. Hold-to-expiry mode (ride winners to $1.00 settlement)
    2. Profit target reached (scalp exit)
    3. Stop loss hit
    4. Position timeout (approaching market expiry)
    5. Market conditions flip

    Uses dual strategy profiles:
    - Up/Down (5m/15m): 2.5% profit / 1.0% stop → R/R = 2.5:1
    - Price Target (≤1hr): 5.0% profit / 2.0% stop → R/R = 2.5:1
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg

    async def check_exit(
        self,
        trade: TradeRecord,
        contract: MarketContract,
        btc: BTCPrice
    ) -> tuple[bool, float, str]:
        """
        Returns: (should_exit, exit_price, reason)
        """
        current_price = (
            contract.yes_price if trade.side == "YES"
            else contract.no_price
        )
        unrealized_pnl_pct = (current_price - trade.entry_price) / trade.entry_price
        seconds_held = time.time() - trade.entry_time
        seconds_to_expiry = contract.seconds_to_expiry

        # Select strategy profile based on market type
        if contract.market_type == "price_target":
            profit_target = self.cfg.price_target_profit      # 5%
            stop_loss = self.cfg.price_target_stop             # 2%
            max_hold = self.cfg.price_target_max_hold          # 3600s
        else:
            profit_target = self.cfg.min_profit_target         # 2.5%
            stop_loss = self.cfg.stop_loss_pct                 # 1.0%
            max_hold = self.cfg.max_hold_seconds               # 270s

        # === HOLD TO EXPIRY MODE ===
        # If price moved strongly in our favor, skip scalp target and ride to $1.00
        # Worst case: trailing stop at entry price = break-even (no loss)
        in_our_favor = (
            (trade.side == "YES" and current_price >= self.cfg.hold_to_expiry_threshold) or
            (trade.side == "NO" and current_price >= self.cfg.hold_to_expiry_threshold)
        )
        momentum_confirms = (
            (trade.side == "YES" and btc.change_1m > 0) or
            (trade.side == "NO" and btc.change_1m < 0)
        )
        hold_to_expiry = (
            in_our_favor
            and momentum_confirms
            and unrealized_pnl_pct > 0
            and seconds_to_expiry > 60
        )

        if hold_to_expiry:
            logger.debug(
                f"🎯 HOLD TO EXPIRY | {trade.trade_id} | {trade.side} @ "
                f"${current_price:.4f} (entry ${trade.entry_price:.4f}) | "
                f"{seconds_to_expiry:.0f}s remaining"
            )
            # Break-even trailing stop: exit if price drops back to entry
            if current_price <= trade.entry_price:
                return True, current_price, "hold_expiry_trailing_stop"
            # Near-expiry final exit: let market settle
            if seconds_to_expiry < 15:
                return True, current_price, "hold_expiry_final_exit"
            # Strong reversal: exit to protect gains
            strong_reversal = (
                (trade.side == "YES" and btc.change_1m < -0.005) or
                (trade.side == "NO" and btc.change_1m > 0.005)
            )
            if strong_reversal:
                return True, current_price, "hold_expiry_reversal"
            # HOLD — don't exit, ride to settlement
            return False, current_price, ""

        # === NORMAL SCALP EXIT LOGIC ===

        # 1. Profit target (2.5% for up/down, 5% for price targets)
        if unrealized_pnl_pct >= profit_target:
            return True, current_price, f"profit_target ({unrealized_pnl_pct*100:.2f}%)"

        # 2. Stop loss (1.0% for up/down, 2.0% for price targets)
        if unrealized_pnl_pct <= -stop_loss:
            return True, current_price, f"stop_loss ({unrealized_pnl_pct*100:.2f}%)"

        # 3. Timeout: close 30 seconds before expiry
        if seconds_to_expiry < 30:
            return True, current_price, "expiry_approaching"

        # 4. Max hold time (270s for up/down, 3600s for price targets)
        if seconds_held > max_hold:
            return True, current_price, "max_hold_timeout"

        # 5. Signal reversal: price moved against us strongly
        signal_reversed = (
            (trade.side == "YES" and btc.change_1m < -0.003) or
            (trade.side == "NO" and btc.change_1m > 0.003)
        )
        if signal_reversed and unrealized_pnl_pct < 0:
            return True, current_price, "signal_reversal"

        return False, current_price, ""


# ═══════════════════════════════════════════════════════════
#  MAIN BOT ORCHESTRATOR
# ═══════════════════════════════════════════════════════════

class PolymarketBot:
    def __init__(self, cfg: Config, mode: str = "paper"):
        self.cfg = cfg
        self.mode = mode

        # Initialize components
        self.btc_feed      = BTCPriceFeed(cfg)
        self.chainlink     = ChainlinkFeed()
        self.hl_feed       = HyperliquidPriceFeed()
        self.poly_feed     = PolymarketFeed(cfg)
        self.signal_engine = SignalEngine(cfg)
        self.enhanced_feed = EnhancedSignalFeed()
        self.risk          = RiskManager(cfg, cfg.initial_capital)
        self.position_monitor = PositionMonitor(cfg)
        self.claude_bot = ClaudeBot(cfg)

        # Trader (paper or live)
        if mode == "live" and not cfg.paper_trading:
            self.trader = LiveTrader(cfg)
            logger.warning("🔴 LIVE TRADING MODE — Real money at risk!")
        else:
            self.trader = PaperTrader(cfg)
            logger.info("📋 PAPER TRADING MODE — No real money used")

        self._running = False
        self._paused = False
        self._open_trades: dict[str, TradeRecord] = {}
        self._scan_count = 0
        self._start_time = time.time()
        self._last_status_time = time.time()
        self._pnl_history: list[float] = []
        self._ai_analysis: dict = {"regime": "unknown", "trade_multiplier": 1.0}

    async def start(self):
        self._running = True
        logger.info("=" * 60)
        logger.info("🚀 POLYMARKET BTC BOT STARTING")
        logger.info(f"   Mode: {'PAPER' if self.cfg.paper_trading else '🔴 LIVE'}")
        logger.info(f"   Capital: ${self.cfg.initial_capital:.2f}")
        logger.info(f"   Max trade: {self.cfg.max_trade_pct*100}% per trade")
        logger.info(f"   Target latency: {self.cfg.target_latency_ms}ms")
        logger.info("=" * 60)

        # Start data feeds concurrently
        await asyncio.gather(
            self.btc_feed.connect(),
            self.chainlink.start(),
            self.hl_feed.start(),
            self.poly_feed.start(),
            self.enhanced_feed.start(),
            self._main_loop(),
            self._status_reporter(),
            self._state_writer(),
            self._command_reader(),
        )

    async def _main_loop(self):
        """Core trading loop — runs every ~100ms."""
        # Wait for initial data
        logger.info("⏳ Waiting for market data feeds...")
        await asyncio.sleep(5)

        while self._running:
            loop_start = time.perf_counter()

            try:
                await self._scan_and_trade()
                await self._manage_positions()
            except Exception as e:
                logger.error(f"Main loop error: {e}")

            # Maintain ~100ms loop cycle
            elapsed = (time.perf_counter() - loop_start) * 1000
            sleep_time = max(0, (self.cfg.target_latency_ms - elapsed) / 1000)
            await asyncio.sleep(sleep_time)

    async def _state_writer(self):
        """Write bot state to JSON every second so the dashboard can read it."""
        while self._running:
            try:
                btc = self.btc_feed.current
                open_list = []
                for t in self._open_trades.values():
                    contract = self.poly_feed.markets.get(t.condition_id)
                    current_price = (
                        (contract.yes_price if t.side == "YES" else contract.no_price)
                        if contract else t.entry_price
                    )
                    open_list.append({
                        **asdict(t),
                        "current_price": current_price,
                        "question": contract.question if contract else "",
                    })

                # Build active market signals for dashboard terminal panel
                active_markets = self.poly_feed.get_active_markets()
                markets_info = []
                for m in active_markets[:5]:
                    if btc:
                        sig, edge, conf = self.signal_engine.generate_signal(m, btc)
                    else:
                        sig, edge, conf = Signal.HOLD, 0.0, 0.0
                    in_position = any(t.condition_id == m.condition_id for t in self._open_trades.values())
                    markets_info.append({
                        "condition_id": m.condition_id,
                        "question": m.question,
                        "yes_price": round(m.yes_price, 4),
                        "no_price": round(m.no_price, 4),
                        "seconds_to_expiry": max(0, round(m.seconds_to_expiry)),
                        "signal": sig.value,
                        "edge_pct": round(edge * 100, 3),
                        "confidence": round(conf, 3),
                        "in_position": in_position,
                    })

                state = {
                    "running": self._running,
                    "paused": self._paused,
                    "mode": self.mode,
                    "capital": self.risk.capital,
                    "start_capital": self.risk.daily_start_capital,
                    "btc_price": btc.price if btc else 0.0,
                    "btc_change_1m": btc.change_1m if btc else 0.0,
                    "btc_change_5m": btc.change_5m if btc else 0.0,
                    "chainlink_price":  self.chainlink.price,
                    "hl_price":         round(self.hl_feed.price, 2),
                    "hl_funding":       round(self.hl_feed.funding * 100, 5),   # % per hour
                    "hl_oi_b":          round(self.hl_feed.open_interest / 1e9, 3),  # billions USD
                    "cvd_signal":       round(self.enhanced_feed.cvd_signal,     2),
                    "liq_signal":       round(self.enhanced_feed.liq_signal,     2),
                    "funding_signal":   round(self.enhanced_feed.funding_signal, 2),
                    "oi_signal":        round(self.enhanced_feed.oi_signal,      2),
                    "wins": self.risk.wins_today,
                    "losses": self.risk.losses_today,
                    "open_trades": open_list,
                    "active_markets": markets_info,
                    "markets_tracked": len(active_markets),
                    "last_latency_ms": self.btc_feed.latency_ms,
                    "scan_count": self._scan_count,
                    "consecutive_losses": self.risk.consecutive_losses,
                    "claude_enabled": self.cfg.claude_enabled,
                    "claude_regime": self._ai_analysis.get("regime", "unknown"),
                    "claude_multiplier": self._ai_analysis.get("trade_multiplier", 1.0),
                    "uptime_seconds": int(time.time() - self._start_time),
                    "pnl_history": self._pnl_history[-200:],
                    "last_updated": datetime.now(timezone.utc).strftime("%H:%M:%S UTC"),
                }
                Path(self.cfg.data_dir).mkdir(exist_ok=True)
                with open(f"{self.cfg.data_dir}/bot_state.json", "w") as f:
                    json.dump(state, f)
            except Exception as e:
                logger.debug(f"State write error: {e}")
            await asyncio.sleep(1)

    async def _command_reader(self):
        """Read commands written by the dashboard (pause/stop/set_claude)."""
        cmd_file = f"{self.cfg.data_dir}/bot_commands.json"
        # Ignore any commands that existed before this bot session started.
        # This prevents stale stop/pause commands from a previous run killing the bot on startup.
        last_ts: float = self._start_time
        while self._running:
            try:
                if os.path.exists(cmd_file):
                    with open(cmd_file) as f:
                        cmd = json.load(f)
                    ts = cmd.get("timestamp", 0.0)
                    if ts > last_ts:
                        last_ts = ts
                        action = cmd.get("action", "")
                        if action == "pause":
                            self._paused = True
                            logger.info("⏸  Bot paused via dashboard")
                        elif action == "start":
                            self._paused = False
                            logger.info("▶  Bot resumed via dashboard")
                        elif action == "stop":
                            logger.info("■  Stop command received from dashboard")
                            self.stop()
                        elif action == "set_claude":
                            self.cfg.claude_enabled = bool(cmd.get("value", False))
                            logger.info(f"🤖 ClaudeBot {'enabled' if self.cfg.claude_enabled else 'disabled'} via dashboard")
            except Exception as e:
                logger.debug(f"Command read error: {e}")
            await asyncio.sleep(1)

    async def _scan_and_trade(self):
        """Scan markets and enter new positions if signal detected."""
        if not self.btc_feed.current:
            return

        if self._paused:
            return

        can_trade, _ = self.risk.can_trade()
        if not can_trade:
            return

        btc = self.btc_feed.current
        markets = self.poly_feed.get_active_markets()

        if not markets:
            total = len(self.poly_feed.markets)
            if self._scan_count % 50 == 0:  # Log every ~5s
                logger.debug(f"No tradeable markets (total tracked: {total}, need 30–300s to expiry)")
            return

        # Get AI market regime (async, cached)
        self._ai_analysis = await self.claude_bot.analyze_market_regime(btc)
        trade_multiplier = self._ai_analysis.get("trade_multiplier", 1.0)

        # Limit concurrent open positions to 3
        if len(self._open_trades) >= 3:
            return

        for contract in markets:
            # Don't double-up on same market
            if any(t.condition_id == contract.condition_id
                   for t in self._open_trades.values()):
                continue

            signal, edge, confidence = self.signal_engine.generate_signal(
                contract, btc,
                cvd_signal=self.enhanced_feed.cvd_signal,
                liq_signal=self.enhanced_feed.liq_signal,
                funding_signal=self.enhanced_feed.funding_signal,
                oi_signal=self.enhanced_feed.oi_signal,
                chainlink_price=self.chainlink.price,
            )

            if signal == Signal.HOLD:
                logger.debug(
                    f"HOLD | edge={edge*100:.3f}% (need {self.cfg.min_edge_required*100:.1f}%) | "
                    f"yes={contract.yes_price:.3f} | expiry={contract.seconds_to_expiry:.0f}s"
                )
                continue

            # Apply AI regime multiplier to confidence
            adjusted_confidence = min(confidence * trade_multiplier, 1.0)

            # Calculate position size
            position_size = self.risk.calculate_position_size(adjusted_confidence)
            entry_price = (
                contract.yes_price if signal == Signal.YES
                else contract.no_price
            )
            shares = position_size / entry_price

            logger.debug(
                f"Signal: {signal.value} | Edge: {edge*100:.3f}% | "
                f"Conf: {adjusted_confidence:.2f} | "
                f"${position_size:.2f} | Regime: {self._ai_analysis.get('regime','?')}"
            )

            # Enter trade
            trade = await self.trader.enter(contract, signal, shares, self.risk)
            if trade:
                self._open_trades[trade.trade_id] = trade

            self._scan_count += 1
            # Only enter one trade per scan cycle
            break

    async def _manage_positions(self):
        """Check all open positions for exit conditions."""
        if not self.btc_feed.current:
            return

        trades_to_close = []
        btc = self.btc_feed.current

        for trade in self._open_trades.values():
            contract = self.poly_feed.markets.get(trade.condition_id)
            if not contract:
                # Market no longer tracked, force exit
                trades_to_close.append((trade, 0.5, "market_not_found"))
                continue

            should_exit, exit_price, reason = await self.position_monitor.check_exit(
                trade, contract, btc
            )

            if should_exit:
                trades_to_close.append((trade, exit_price, reason))

        # Execute exits
        for trade, exit_price, reason in trades_to_close:
            await self.trader.exit(trade, exit_price, self.risk, reason)
            if trade.trade_id in self._open_trades:
                del self._open_trades[trade.trade_id]

    async def _status_reporter(self):
        """Print status summary every 30 seconds and track P&L history."""
        await asyncio.sleep(10)
        while self._running:
            btc_price = self.btc_feed.current.price if self.btc_feed.current else 0
            markets = len(self.poly_feed.get_active_markets())

            logger.info(
                f"📊 STATUS | BTC: ${btc_price:,.2f} | "
                f"Markets: {markets} | "
                f"Open: {len(self._open_trades)} | "
                f"Scans: {self._scan_count} | "
                f"{self.risk.report()}"
            )

            # Track P&L history for dashboard chart (capital delta from start)
            pnl_now = self.risk.capital - self.risk.daily_start_capital
            self._pnl_history.append(round(pnl_now, 4))

            # Print paper trade summary
            if isinstance(self.trader, PaperTrader):
                summary = self.trader.summary()
                if summary.get("total_trades", 0) > 0:
                    logger.info(
                        f"📈 PAPER P&L | Trades: {summary['total_trades']} | "
                        f"Win Rate: {summary['win_rate']*100:.1f}% | "
                        f"Total: ${summary['total_pnl']:+.4f} | "
                        f"Avg Win: ${summary['avg_win']:+.4f}"
                    )

            await asyncio.sleep(30)

    def stop(self):
        self._running = False
        self.btc_feed.stop()
        if isinstance(self.trader, LiveTrader):
            asyncio.create_task(self.trader.close())
        logger.info("🛑 Bot stopped")


# ═══════════════════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════════════════

async def main(mode: str):
    cfg = Config()
    setup_logging(cfg)

    bot = PolymarketBot(cfg, mode=mode)

    # Graceful shutdown on Ctrl+C
    # add_signal_handler is Unix-only; on Windows use signal.signal() instead
    try:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, bot.stop)
    except (NotImplementedError, AttributeError):
        # Windows — add_signal_handler not supported; Ctrl+C handled by KeyboardInterrupt
        signal.signal(signal.SIGINT, lambda *_: bot.stop())

    try:
        await bot.start()
    except KeyboardInterrupt:
        bot.stop()
    finally:
        # Print final summary
        if isinstance(bot.trader, PaperTrader):
            summary = bot.trader.summary()
            logger.info("=" * 50)
            logger.info("📋 FINAL PAPER TRADING SUMMARY")
            for k, v in summary.items():
                logger.info(f"   {k}: {v}")
            logger.info("=" * 50)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Polymarket BTC HFT Bot")
    parser.add_argument(
        "--mode",
        choices=["paper", "live"],
        default="paper",
        help="Trading mode (default: paper)"
    )
    args = parser.parse_args()

    print(f"""
╔══════════════════════════════════════════╗
║   POLYMARKET BTC BOT v1.0                ║
║   Mode: {'PAPER TRADING (safe)' if args.mode == 'paper' else '🔴 LIVE TRADING'}          ║
║   Target: 0.10%+ profit per trade        ║
╚══════════════════════════════════════════╝
    """)

    # Windows: ProactorEventLoop has poor WebSocket support.
    # SelectorEventLoop is required for reliable WebSocket connections.
    loop = asyncio.SelectorEventLoop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(main(args.mode))
