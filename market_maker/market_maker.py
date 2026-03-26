"""
╔══════════════════════════════════════════════════════════════╗
║     POLYMARKET MARKET MAKER BOT                              ║
║     Strategy: Two-sided quoting with inventory management    ║
║     Profit: Bid-ask spread capture (1-4¢ per round trip)     ║
║     Risk: Inventory skew + adverse selection                 ║
║     Mode: Paper Trading (set PAPER_TRADING=false to live)    ║
╚══════════════════════════════════════════════════════════════╝

MARKET MAKING 101:
  You post BUY orders slightly below fair value and SELL orders slightly above.
  When someone trades against both sides, you capture the spread.

  Example on a BTC 5-min YES contract trading at $0.50 fair value:
    Your BID: $0.48  (you buy if someone sells to you)
    Your ASK: $0.52  (you sell if someone buys from you)
    Spread captured: $0.04 per share per round trip

  You don't predict direction — you profit from the GAP between bid and ask.

CORE RISKS:
  1. Inventory risk: If price moves against you before both sides fill
  2. Adverse selection: Informed traders pick off your stale quotes
  3. Expiry risk: Holding inventory when market resolves to $0 or $1

MITIGATIONS:
  1. Inventory skew: Shift quotes to encourage fills that reduce inventory
  2. Volatility spread: Widen spread when price is moving fast
  3. Hard inventory limits: Cancel all quotes when position exceeds max
  4. Expiry cutoff: Stop quoting 60s before market resolution
"""

import asyncio
import json
import math
import time
import os
import sys
import re
import signal
import argparse
from collections import deque
from datetime import datetime, timezone
from typing import Optional
from dataclasses import dataclass, field, asdict
from pathlib import Path
from dotenv import load_dotenv

# ── Try uvloop for faster event loop (Linux only) ──
try:
    import uvloop
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
except ImportError:
    pass

import websockets
import aiohttp
from loguru import logger

# ── Optional: Polymarket CLOB client for live trading ──
try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import OrderArgs, BUY, SELL
    CLOB_AVAILABLE = True
except ImportError:
    CLOB_AVAILABLE = False

load_dotenv()

# ── BTC market discovery (same regex as your directional bot) ──
_BTC_SLUG_RE = re.compile(
    r"btc[-_]?(up[-_]?down|updown|higher|lower|prediction)", re.IGNORECASE
)
_BTC_TITLE_RE = re.compile(
    r"(?:btc|bitcoin).*?"
    r"(?:up\s*(?:or|/|,)\s*down|higher\s*or\s*lower|above\s*or\s*below|"
    r"updown|(?:\d+)\s*[-\s]?(?:min|minute|m)\b)",
    re.IGNORECASE
)


# ═══════════════════════════════════════════════════════════
#  CONFIGURATION
# ═══════════════════════════════════════════════════════════

@dataclass
class MMConfig:
    """
    Market Maker configuration — all the knobs you can turn.

    The most important parameters are the spread settings and inventory limits.
    Everything else is infrastructure plumbing.
    """

    # ── Credentials (only needed for live trading) ──
    api_key: str = field(default_factory=lambda: os.getenv("POLYMARKET_API_KEY", ""))
    api_secret: str = field(default_factory=lambda: os.getenv("POLYMARKET_API_SECRET", ""))
    api_passphrase: str = field(default_factory=lambda: os.getenv("POLYMARKET_API_PASSPHRASE", ""))
    private_key: str = field(default_factory=lambda: os.getenv("POLYMARKET_PRIVATE_KEY", ""))

    # ── Trading Mode ──
    paper_trading: bool = field(default_factory=lambda: os.getenv("PAPER_TRADING", "true").lower() == "true")
    initial_capital: float = field(default_factory=lambda: float(os.getenv("INITIAL_CAPITAL", "1000")))

    # ═══════════════════════════════════════════════════════
    #  SPREAD SETTINGS — This is where your profit comes from
    # ═══════════════════════════════════════════════════════
    #
    # base_spread_pct:
    #   The minimum distance between your bid and ask, expressed as a
    #   fraction of the fair value. A spread of 0.04 means your bid is
    #   2¢ below fair value and your ask is 2¢ above (total 4¢ gap).
    #
    #   Too tight (< 0.02): You'll get filled constantly but adverse
    #   selection eats your profits — informed traders pick you off.
    #
    #   Too wide (> 0.08): You rarely get filled because other market
    #   makers undercut you. You're just sitting there with open orders.
    #
    #   Sweet spot for Polymarket BTC markets: 0.03–0.05
    #
    base_spread_pct: float = 0.04   # 4¢ total spread on a $1.00 contract

    # max_spread_pct:
    #   When volatility spikes, the bot widens the spread to protect
    #   itself. This is the upper limit — it won't go wider than this.
    #   A wider spread means less adverse selection risk but fewer fills.
    #
    max_spread_pct: float = 0.10    # 10¢ max spread during high volatility

    # volatility_spread_multiplier:
    #   How aggressively the spread widens when BTC is moving fast.
    #   Higher = more defensive (wider spreads during moves).
    #   Lower = more aggressive (tighter spreads, more fills, more risk).
    #
    #   The formula: effective_spread = base_spread + (btc_1m_change * multiplier)
    #
    volatility_spread_multiplier: float = 5.0

    # ═══════════════════════════════════════════════════════
    #  INVENTORY MANAGEMENT — This is how you control risk
    # ═══════════════════════════════════════════════════════
    #
    # Inventory = your net position on one side of a market.
    # +50 shares YES means you're LONG YES (you profit if YES wins).
    # -30 shares YES means you're SHORT YES (you sold more than you bought).
    #
    # The goal is to keep inventory near zero — you're a shopkeeper,
    # not a speculator. Every share you hold is risk.
    #
    max_inventory_shares: float = 100.0   # Hard limit — cancel all quotes if exceeded
    inventory_skew_factor: float = 0.002  # How much to shift quotes per share of inventory

    # When inventory exceeds this fraction of max, start aggressively
    # pricing to reduce it (wider on the side you're long, tighter
    # on the side you want fills)
    inventory_panic_threshold: float = 0.75  # 75% of max → start aggressive rebalancing

    # ═══════════════════════════════════════════════════════
    #  ORDER SIZING
    # ═══════════════════════════════════════════════════════
    #
    # quote_size_shares:
    #   How many shares per quote. Larger = more capital at risk per
    #   fill but more spread captured. Smaller = less risk, more
    #   orders needed to make the same profit.
    #
    #   At $0.50 per share, 20 shares = $10 per side = $20 total deployed.
    #
    quote_size_shares: float = 20.0       # Shares per order
    max_capital_deployed_pct: float = 0.15  # Never deploy more than 15% of capital in quotes

    # ═══════════════════════════════════════════════════════
    #  TIMING & SAFETY
    # ═══════════════════════════════════════════════════════
    quote_refresh_ms: int = 500           # Re-quote every 500ms (2x per second)
    expiry_cutoff_seconds: int = 60       # Stop quoting 60s before market expires
    min_market_seconds: int = 120         # Don't quote markets with < 2 min remaining

    # ═══════════════════════════════════════════════════════
    #  DAILY RISK LIMITS
    # ═══════════════════════════════════════════════════════
    daily_loss_limit_pct: float = 0.05    # Stop if down 5% on the day
    max_adverse_fills: int = 10           # If 10 fills in a row lose money, pause 5 min
    adverse_fill_pause: int = 300         # Seconds to pause after adverse fill streak

    # ═══════════════════════════════════════════════════════
    #  API ENDPOINTS
    # ═══════════════════════════════════════════════════════
    gamma_api_url: str = "https://gamma-api.polymarket.com"
    clob_rest_url: str = "https://clob.polymarket.com"
    clob_ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    binance_ws_url: str = "wss://stream.binance.com:9443/ws/btcusdt@ticker"
    kraken_ws_url: str = "wss://ws.kraken.com"

    # ── Files ──
    log_dir: str = "logs"
    data_dir: str = "data"
    mm_trades_file: str = "data/mm_trades.json"
    mm_state_file: str = "data/mm_state.json"


# ═══════════════════════════════════════════════════════════
#  DATA MODELS
# ═══════════════════════════════════════════════════════════

@dataclass
class Quote:
    """A single resting order on the book (bid or ask)."""
    order_id: str
    side: str           # "BUY" or "SELL"
    token_id: str       # YES or NO token
    outcome: str        # "YES" or "NO"
    price: float
    size: float
    posted_at: float = field(default_factory=time.time)
    filled: bool = False
    fill_price: float = 0.0
    fill_time: float = 0.0


@dataclass
class Inventory:
    """
    Tracks net position in a market.

    Positive yes_shares = we're long YES (bullish exposure).
    Positive no_shares  = we're long NO (bearish exposure).

    Net exposure = yes_shares - no_shares.
    If net > 0: we profit if YES wins. If net < 0: we profit if NO wins.
    Goal: keep net exposure near zero.
    """
    condition_id: str
    yes_shares: float = 0.0
    no_shares: float = 0.0
    total_spread_captured: float = 0.0
    round_trips: int = 0
    adverse_fills: int = 0          # Consecutive fills that moved against us

    @property
    def net_exposure(self) -> float:
        """Positive = long YES, negative = long NO."""
        return self.yes_shares - self.no_shares

    @property
    def total_shares(self) -> float:
        """Absolute inventory on either side."""
        return abs(self.yes_shares) + abs(self.no_shares)


@dataclass
class MarketContract:
    """A Polymarket binary contract we can quote on."""
    condition_id: str
    question: str
    yes_price: float
    no_price: float
    yes_bid: float = 0.0
    yes_ask: float = 0.0
    no_bid: float = 0.0
    no_ask: float = 0.0
    expiry_ts: float = 0.0
    start_ts: float = 0.0
    yes_token_id: str = ""
    no_token_id: str = ""
    last_updated: float = 0.0

    @property
    def seconds_to_expiry(self) -> float:
        return self.expiry_ts - time.time()

    @property
    def market_spread(self) -> float:
        """Current market spread on YES side."""
        if self.yes_ask > 0 and self.yes_bid > 0:
            return self.yes_ask - self.yes_bid
        return 0.0


@dataclass
class MMStats:
    """Running statistics for the market maker session."""
    start_time: float = field(default_factory=time.time)
    total_quotes_posted: int = 0
    total_fills: int = 0
    total_spread_captured: float = 0.0
    total_inventory_pnl: float = 0.0     # P&L from closing inventory at market
    total_round_trips: int = 0
    markets_quoted: int = 0
    quotes_cancelled: int = 0
    adverse_selection_events: int = 0     # Times a fill immediately moved against us


# ═══════════════════════════════════════════════════════════
#  LOGGING SETUP
# ═══════════════════════════════════════════════════════════

def setup_logging(cfg: MMConfig):
    Path(cfg.log_dir).mkdir(exist_ok=True)
    Path(cfg.data_dir).mkdir(exist_ok=True)

    logger.remove()
    logger.add(
        sys.stderr,
        format="<green>{time:HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | {message}",
        level="INFO", colorize=True,
    )
    logger.add(
        f"{cfg.log_dir}/mm_bot.log",
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level} | {message}",
        level="DEBUG", rotation="100 MB", retention="7 days",
    )
    logger.add(
        f"{cfg.log_dir}/mm_fills.log",
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {message}",
        filter=lambda r: "FILL" in r["message"],
        level="INFO",
    )


# ═══════════════════════════════════════════════════════════
#  BTC PRICE FEED (reused from your directional bot)
# ═══════════════════════════════════════════════════════════

class BTCPriceFeed:
    """
    Real-time BTC price for fair value estimation.
    Tries Binance first, falls back to Kraken.
    """

    def __init__(self, cfg: MMConfig):
        self.cfg = cfg
        self.price: float = 0.0
        self.change_1m: float = 0.0
        self.change_5m: float = 0.0
        self.volatility_1m: float = 0.0   # Rolling 1-min standard deviation of returns
        self.volume_ratio: float = 1.0    # Current volume / rolling avg (for weighting momentum)
        self._history: deque[float] = deque(maxlen=300)
        self._returns: deque[float] = deque(maxlen=60)   # 1-min of tick returns
        self._volumes: deque[float] = deque(maxlen=900)  # ~15 min of volume ticks
        self._running = False

    async def connect(self):
        self._running = True
        blocked = await self._connect_binance()
        if blocked and self._running:
            await self._connect_kraken()

    async def _connect_binance(self) -> bool:
        attempts = 0
        backoff = 1.0
        while self._running:
            try:
                async with websockets.connect(
                    self.cfg.binance_ws_url, ping_interval=20, ping_timeout=10
                ) as ws:
                    logger.success("✅ BTC feed connected (Binance)")
                    attempts = 0
                    async for msg in ws:
                        data = json.loads(msg)
                        p = float(data.get("c", 0))
                        v = float(data.get("v", 0))
                        if p > 0:
                            self._update(p, volume=v)
            except websockets.exceptions.InvalidStatus as e:
                status = getattr(e.response, "status_code", None)
                if status in (403, 451):
                    return True
                attempts += 1
                if attempts >= 4:
                    return True
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
            except Exception:
                attempts += 1
                if attempts >= 4:
                    return True
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
        return False

    async def _connect_kraken(self):
        backoff = 1.0
        while self._running:
            try:
                async with websockets.connect(self.cfg.kraken_ws_url, ping_interval=20) as ws:
                    await ws.send(json.dumps({
                        "event": "subscribe",
                        "pair": ["XBT/USD"],
                        "subscription": {"name": "ticker"},
                    }))
                    logger.success("✅ BTC feed connected (Kraken)")
                    async for msg in ws:
                        data = json.loads(msg)
                        if isinstance(data, list) and len(data) >= 2:
                            t = data[1]
                            if isinstance(t, dict) and "c" in t:
                                p = float(t["c"][0])
                                v = float(t.get("v", [0])[0]) if "v" in t else 0
                                if p > 0:
                                    self._update(p, volume=v)
            except Exception:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    def _update(self, price: float, volume: float = 0.0):
        # Track tick-to-tick returns for volatility estimation
        if self.price > 0:
            ret = (price - self.price) / self.price
            self._returns.append(ret)

        self.price = price
        self._history.append(price)
        h = list(self._history)
        self.change_1m = (price - h[-60]) / h[-60] if len(h) >= 60 else 0.0
        self.change_5m = (price - h[0]) / h[0] if len(h) >= 300 else 0.0

        # Rolling 1-minute realized volatility (standard deviation of returns)
        if len(self._returns) >= 10:
            mean = sum(self._returns) / len(self._returns)
            var = sum((r - mean) ** 2 for r in self._returns) / len(self._returns)
            self.volatility_1m = var ** 0.5
        else:
            self.volatility_1m = 0.001  # Default low vol

        # Volume tracking for momentum weighting
        # volume_ratio > 1 means above-average volume (stronger signal)
        # volume_ratio < 1 means below-average volume (weaker signal)
        if volume > 0:
            self._volumes.append(volume)
            if len(self._volumes) >= 10:
                avg_vol = sum(self._volumes) / len(self._volumes)
                self.volume_ratio = min(max(volume / avg_vol, 0.3), 2.5) if avg_vol > 0 else 1.0

    def stop(self):
        self._running = False


# ═══════════════════════════════════════════════════════════
#  POLYMARKET FEED (market discovery + orderbook)
# ═══════════════════════════════════════════════════════════

class PolymarketFeed:
    """
    Discovers BTC prediction markets and tracks live orderbook prices.
    For market making, we need the FULL orderbook (bid/ask), not just mid price.
    """

    def __init__(self, cfg: MMConfig):
        self.cfg = cfg
        self.markets: dict[str, MarketContract] = {}
        self._running = False
        self._session: Optional[aiohttp.ClientSession] = None

    async def start(self):
        self._session = aiohttp.ClientSession()
        self._running = True
        asyncio.create_task(self._poll_markets())
        asyncio.create_task(self._poll_orderbooks())

    async def _poll_markets(self):
        """Discover active BTC 5m/15m markets every 30s."""
        while self._running:
            try:
                url = f"{self.cfg.gamma_api_url}/events"
                params = {"active": "true", "closed": "false", "limit": 200,
                          "order": "endDate", "ascending": "true"}
                async with self._session.get(url, params=params) as resp:
                    if resp.status == 200:
                        await self._process_events(await resp.json())
            except Exception as e:
                logger.warning(f"Market poll error: {e}")
            await asyncio.sleep(30)

    async def _process_events(self, events):
        now = time.time()
        new_count = 0
        for event in events:
            slug = (event.get("slug", "") or event.get("ticker", "")).lower()
            title = (event.get("title", "") or event.get("question", "")).lower()
            if not (_BTC_SLUG_RE.search(slug) or _BTC_TITLE_RE.search(title)):
                continue
            if event.get("closed") or not event.get("active"):
                continue

            for m in event.get("markets", []):
                cid = m.get("conditionId", "")
                if not cid:
                    continue
                try:
                    expiry_ts = datetime.fromisoformat(
                        m.get("endDate", "").replace("Z", "+00:00")
                    ).timestamp()
                except Exception:
                    continue
                try:
                    start_ts = datetime.fromisoformat(
                        m.get("startDate", "").replace("Z", "+00:00")
                    ).timestamp()
                except Exception:
                    start_ts = expiry_ts - 300

                duration = expiry_ts - start_ts
                if duration > 1800 or expiry_ts <= now:
                    continue

                # Parse prices and token IDs
                try:
                    prices = json.loads(m.get("outcomePrices", "[0.5,0.5]"))
                except Exception:
                    prices = [0.5, 0.5]
                try:
                    token_ids = json.loads(m.get("clobTokenIds", "[]"))
                except Exception:
                    token_ids = []

                contract = MarketContract(
                    condition_id=cid,
                    question=m.get("question", ""),
                    yes_price=float(prices[0]) if prices else 0.5,
                    no_price=float(prices[1]) if len(prices) > 1 else 0.5,
                    expiry_ts=expiry_ts,
                    start_ts=start_ts,
                    yes_token_id=str(token_ids[0]) if len(token_ids) > 0 else "",
                    no_token_id=str(token_ids[1]) if len(token_ids) > 1 else "",
                )

                if cid not in self.markets:
                    new_count += 1
                self.markets[cid] = contract

        if new_count:
            logger.info(f"📡 Discovered {new_count} new BTC markets (total: {len(self.markets)})")

    async def _poll_orderbooks(self):
        """
        Fetch full orderbook (bid/ask) for each tracked market every 2s.
        This is CRITICAL for market making — we need to know where other
        market makers are quoting so we can price competitively.
        """
        while self._running:
            for cid, contract in list(self.markets.items()):
                if contract.seconds_to_expiry < 10:
                    continue
                try:
                    # Fetch YES side orderbook
                    if contract.yes_token_id:
                        b, a = await self._fetch_book(contract.yes_token_id)
                        contract.yes_bid = b
                        contract.yes_ask = a
                        contract.yes_price = (b + a) / 2 if b > 0 and a > 0 else contract.yes_price
                    # Fetch NO side orderbook
                    if contract.no_token_id:
                        b, a = await self._fetch_book(contract.no_token_id)
                        contract.no_bid = b
                        contract.no_ask = a
                        contract.no_price = (b + a) / 2 if b > 0 and a > 0 else contract.no_price
                    contract.last_updated = time.time()
                except Exception as e:
                    logger.debug(f"Orderbook fetch error for {cid[:8]}: {e}")
            await asyncio.sleep(2)

    async def _fetch_book(self, token_id: str) -> tuple[float, float]:
        """Returns (best_bid, best_ask) for a token."""
        try:
            url = f"{self.cfg.clob_rest_url}/book"
            async with self._session.get(url, params={"token_id": token_id}, timeout=aiohttp.ClientTimeout(total=4)) as r:
                data = await r.json()
                bids = data.get("bids", [])
                asks = data.get("asks", [])
                best_bid = float(bids[0]["price"]) if bids else 0.0
                best_ask = float(asks[0]["price"]) if asks else 0.0
                return best_bid, best_ask
        except Exception:
            return 0.0, 0.0

    def get_quoteable_markets(self, cfg: MMConfig) -> list[MarketContract]:
        """Return markets suitable for market making (enough time, has token IDs)."""
        result = []
        for m in self.markets.values():
            if m.seconds_to_expiry < cfg.min_market_seconds:
                continue
            if m.seconds_to_expiry > 1200:
                continue
            if not m.yes_token_id or not m.no_token_id:
                continue
            result.append(m)
        return result

    async def stop(self):
        self._running = False
        if self._session:
            await self._session.close()


# ═══════════════════════════════════════════════════════════
#  FAIR VALUE ENGINE
# ═══════════════════════════════════════════════════════════

class FairValueEngine:
    """
    Estimates the "true" probability that BTC will be up at expiry.

    This is the CENTER of your quotes. If fair value = 0.52, you bid at
    0.50 and ask at 0.54 (with 4¢ spread). The better your fair value
    estimate, the less adverse selection you face.

    For market making, we want a LESS aggressive fair value than the
    directional bot — we're not trying to predict, we're trying to
    sit in the middle and capture spread. So the adjustments are smaller.
    """

    def estimate(self, btc_price: float, btc_change_1m: float,
                 btc_change_5m: float, contract: MarketContract,
                 volume_ratio: float = 1.0) -> float:
        """
        Returns fair value for YES (0.0 to 1.0).

        Uses a dampened version of BTC momentum — we don't want our quotes
        to swing wildly because a market maker's edge comes from STABILITY,
        not from being right about direction.
        """
        # Start at 0.50 (no directional bias)
        base = 0.50

        # Volume-weight the momentum: high volume moves are more meaningful,
        # low volume moves are more likely noise. volume_ratio > 1 amplifies,
        # < 1 dampens. Clamped to [0.3, 2.5] in BTCPriceFeed.
        vol_weighted_1m = btc_change_1m * volume_ratio
        vol_weighted_5m = btc_change_5m * volume_ratio

        # Small momentum adjustment (dampened compared to directional bot)
        # Uses tanh for smooth nonlinear scaling: noise (0.01%) is suppressed,
        # real moves (0.1%+) get proportional weight, extreme moves are capped.
        # Sensitivity 150 (dampened vs directional bot's 200) with 0.05 cap.
        adj_1m = math.tanh(vol_weighted_1m * 150) * 0.05
        adj_5m = math.tanh(vol_weighted_5m * 75) * 0.03

        # Weight 1m more than 5m (recent momentum matters more for short-term)
        momentum = adj_1m * 0.7 + adj_5m * 0.3

        # Also consider where the market is currently pricing it — if the market
        # consensus is far from 0.50, there's probably a reason. We blend our
        # momentum estimate with the market's current mid-price.
        market_mid = contract.yes_price
        market_weight = 0.4   # Trust the market 40%, our model 60%

        fair_value = (base + momentum) * (1 - market_weight) + market_mid * market_weight
        return min(max(fair_value, 0.05), 0.95)


# ═══════════════════════════════════════════════════════════
#  QUOTE ENGINE — The Heart of Market Making
# ═══════════════════════════════════════════════════════════

class QuoteEngine:
    """
    Generates bid/ask quotes for a market.

    This is where the magic happens. The quote engine takes:
      - Fair value (from FairValueEngine)
      - Current inventory (how much we're holding)
      - Current volatility (how fast BTC is moving)
      - Market microstructure (where other quotes are)

    And produces:
      - A bid price and size (where we'll buy)
      - An ask price and size (where we'll sell)

    KEY CONCEPT: Inventory Skew
    ═══════════════════════════
    If you're holding +50 YES shares (long YES), you WANT someone to
    buy them from you. So you make your ASK price slightly cheaper
    (more attractive to buyers) and your BID price slightly worse
    (less attractive to sellers). This encourages fills that REDUCE
    your inventory.

    The math: skew = inventory * skew_factor
      Bid  = fair_value - half_spread - skew  (worse bid when long)
      Ask  = fair_value + half_spread - skew  (better ask when long)

    If you're SHORT (negative inventory), the skew flips — your bid
    gets tighter and your ask gets wider, encouraging buying.
    """

    def __init__(self, cfg: MMConfig):
        self.cfg = cfg

    def generate_quotes(
        self,
        fair_value: float,
        inventory: Inventory,
        volatility: float,
        contract: MarketContract,
    ) -> dict:
        """
        Returns a dict with YES and NO side quotes:
        {
            "yes_bid": float, "yes_ask": float, "yes_size": float,
            "no_bid": float,  "no_ask": float,  "no_size": float,
            "effective_spread": float,
            "skew": float,
        }
        """
        # ── Step 1: Calculate effective spread ──
        # Start with base spread, widen based on volatility
        vol_adjustment = volatility * self.cfg.volatility_spread_multiplier
        effective_spread = min(
            self.cfg.base_spread_pct + vol_adjustment,
            self.cfg.max_spread_pct
        )
        half_spread = effective_spread / 2

        # ── Step 2: Calculate inventory skew ──
        # Positive net_exposure = we're long YES → shift quotes to sell YES
        skew = inventory.net_exposure * self.cfg.inventory_skew_factor

        # If inventory is in "panic zone" (>75% of max), double the skew
        if abs(inventory.net_exposure) > self.cfg.max_inventory_shares * self.cfg.inventory_panic_threshold:
            skew *= 2.0
            logger.warning(
                f"⚠️  Inventory panic zone! Net: {inventory.net_exposure:+.1f} | "
                f"Doubling skew to {skew:+.4f}"
            )

        # ── Step 3: Generate YES side quotes ──
        # Bid = where we'll BUY YES shares
        # Ask = where we'll SELL YES shares
        yes_bid = round(max(0.01, fair_value - half_spread - skew), 4)
        yes_ask = round(min(0.99, fair_value + half_spread - skew), 4)

        # Sanity: bid must be below ask
        if yes_bid >= yes_ask:
            yes_bid = round(yes_ask - 0.01, 4)

        # ── Step 4: Generate NO side quotes ──
        # NO fair value = 1 - YES fair value
        no_fair = 1.0 - fair_value
        no_skew = -skew  # Inverse skew for NO side
        no_bid = round(max(0.01, no_fair - half_spread - no_skew), 4)
        no_ask = round(min(0.99, no_fair + half_spread - no_skew), 4)

        if no_bid >= no_ask:
            no_bid = round(no_ask - 0.01, 4)

        # ── Step 5: Size quotes ──
        # Reduce size when inventory is high (don't add to a losing position)
        inventory_pct = abs(inventory.net_exposure) / self.cfg.max_inventory_shares
        size_multiplier = max(0.25, 1.0 - inventory_pct)
        quote_size = round(self.cfg.quote_size_shares * size_multiplier, 1)

        return {
            "yes_bid": yes_bid, "yes_ask": yes_ask, "yes_size": quote_size,
            "no_bid": no_bid, "no_ask": no_ask, "no_size": quote_size,
            "effective_spread": effective_spread,
            "skew": skew,
            "fair_value": fair_value,
        }


# ═══════════════════════════════════════════════════════════
#  PAPER ORDER MANAGER (simulates fills)
# ═══════════════════════════════════════════════════════════

class PaperOrderManager:
    """
    Simulates order posting and fill detection for paper trading.

    In live mode, this would be replaced with actual CLOB API calls.
    The paper version checks if the market price has crossed our
    quote price and simulates a fill.
    """

    def __init__(self, cfg: MMConfig):
        self.cfg = cfg
        self.active_quotes: dict[str, Quote] = {}  # order_id → Quote
        self.fill_history: list[Quote] = []
        self._order_counter = 0

    def post_quote(self, token_id: str, outcome: str, side: str,
                   price: float, size: float) -> Quote:
        """Post a new resting order (paper mode — just records it)."""
        self._order_counter += 1
        order_id = f"MM-{self._order_counter:06d}"

        quote = Quote(
            order_id=order_id,
            side=side,
            token_id=token_id,
            outcome=outcome,
            price=price,
            size=size,
        )
        self.active_quotes[order_id] = quote
        return quote

    def cancel_all(self, token_id: str = ""):
        """Cancel all quotes (or all for a specific token)."""
        to_cancel = []
        for oid, q in self.active_quotes.items():
            if not token_id or q.token_id == token_id:
                to_cancel.append(oid)
        for oid in to_cancel:
            del self.active_quotes[oid]
        return len(to_cancel)

    def check_fills(self, contract: MarketContract) -> list[Quote]:
        """
        Check if any quotes have been filled based on current market prices.

        A BID (buy order) fills when the market ask drops to or below our bid.
        An ASK (sell order) fills when the market bid rises to or above our ask.

        In paper mode, we simulate this by comparing our quote prices to the
        current best bid/ask from the orderbook.
        """
        fills = []
        to_remove = []

        for oid, quote in self.active_quotes.items():
            filled = False

            if quote.outcome == "YES":
                if quote.side == "BUY" and contract.yes_ask > 0 and contract.yes_ask <= quote.price:
                    filled = True
                elif quote.side == "SELL" and contract.yes_bid > 0 and contract.yes_bid >= quote.price:
                    filled = True
            elif quote.outcome == "NO":
                if quote.side == "BUY" and contract.no_ask > 0 and contract.no_ask <= quote.price:
                    filled = True
                elif quote.side == "SELL" and contract.no_bid > 0 and contract.no_bid >= quote.price:
                    filled = True

            if filled:
                quote.filled = True
                quote.fill_price = quote.price  # Paper fills at our price
                quote.fill_time = time.time()
                fills.append(quote)
                to_remove.append(oid)
                self.fill_history.append(quote)

        for oid in to_remove:
            del self.active_quotes[oid]

        return fills


# ═══════════════════════════════════════════════════════════
#  LIVE ORDER MANAGER (real CLOB orders)
# ═══════════════════════════════════════════════════════════

class LiveOrderManager:
    """
    Posts real orders to Polymarket CLOB via py-clob-client.
    Mirrors the PaperOrderManager interface.
    """

    def __init__(self, cfg: MMConfig):
        self.cfg = cfg
        self.client: Optional[ClobClient] = None
        self.active_quotes: dict[str, Quote] = {}
        self.fill_history: list[Quote] = []
        self._order_counter = 0

        if CLOB_AVAILABLE and cfg.private_key:
            try:
                self.client = ClobClient(
                    host=cfg.clob_rest_url, chain_id=137, key=cfg.private_key,
                    creds={"apiKey": cfg.api_key, "secret": cfg.api_secret,
                           "passphrase": cfg.api_passphrase},
                )
                logger.success("✅ CLOB client initialized for market making")
            except Exception as e:
                logger.error(f"CLOB init failed: {e}")

    def post_quote(self, token_id: str, outcome: str, side: str,
                   price: float, size: float) -> Optional[Quote]:
        if not self.client:
            return None
        try:
            order = OrderArgs(
                token_id=token_id, price=price, size=size,
                side=BUY if side == "BUY" else SELL,
            )
            result = self.client.create_and_post_order(order)
            oid = result.get("orderID", f"LIVE-{self._order_counter}")
            self._order_counter += 1
            quote = Quote(
                order_id=oid, side=side, token_id=token_id, outcome=outcome,
                price=price, size=size,
            )
            self.active_quotes[oid] = quote
            return quote
        except Exception as e:
            logger.error(f"Post quote failed: {e}")
            return None

    def cancel_all(self, token_id: str = "") -> int:
        if not self.client:
            return 0
        cancelled = 0
        for oid in list(self.active_quotes.keys()):
            try:
                self.client.cancel(oid)
                del self.active_quotes[oid]
                cancelled += 1
            except Exception:
                pass
        return cancelled

    def check_fills(self, contract: MarketContract) -> list[Quote]:
        """In live mode, fills come from CLOB events. This is a polling fallback."""
        # TODO: Implement WebSocket fill listener for real-time fill detection.
        # For now, we poll active orders and check their status.
        if not self.client:
            return []
        fills = []
        for oid, quote in list(self.active_quotes.items()):
            try:
                order_status = self.client.get_order(oid)
                if order_status and order_status.get("status") == "FILLED":
                    quote.filled = True
                    quote.fill_price = float(order_status.get("fill_price", quote.price))
                    quote.fill_time = time.time()
                    fills.append(quote)
                    self.fill_history.append(quote)
                    del self.active_quotes[oid]
            except Exception:
                pass
        return fills


# ═══════════════════════════════════════════════════════════
#  RISK MANAGER (Market Maker specific)
# ═══════════════════════════════════════════════════════════

class MMRiskManager:
    """
    Risk management tailored for market making.

    Unlike a directional bot, the MM risk manager focuses on:
    1. Inventory limits (don't accumulate too much on one side)
    2. Daily P&L limits (stop if losing too much)
    3. Adverse selection detection (pause if getting picked off repeatedly)
    4. Capital deployment limits (don't tie up all capital in quotes)
    """

    def __init__(self, cfg: MMConfig):
        self.cfg = cfg
        self.capital = cfg.initial_capital
        self.start_capital = cfg.initial_capital
        self.inventories: dict[str, Inventory] = {}    # condition_id → Inventory
        self.daily_pnl: float = 0.0
        self.consecutive_adverse: int = 0
        self.pause_until: float = 0.0

    def get_inventory(self, condition_id: str) -> Inventory:
        if condition_id not in self.inventories:
            self.inventories[condition_id] = Inventory(condition_id=condition_id)
        return self.inventories[condition_id]

    def can_quote(self) -> tuple[bool, str]:
        """Check if we're allowed to post new quotes."""
        if time.time() < self.pause_until:
            remaining = int(self.pause_until - time.time())
            return False, f"Adverse selection pause ({remaining}s)"

        daily_pnl_pct = self.daily_pnl / self.start_capital
        if daily_pnl_pct <= -self.cfg.daily_loss_limit_pct:
            return False, f"Daily loss limit ({daily_pnl_pct*100:.2f}%)"

        return True, "OK"

    def check_inventory_limit(self, inventory: Inventory) -> bool:
        """Returns True if inventory is within limits, False if we should stop quoting."""
        return abs(inventory.net_exposure) < self.cfg.max_inventory_shares

    def record_fill(self, quote: Quote, inventory: Inventory, contract: MarketContract):
        """Process a fill event and update inventory + P&L tracking."""
        if quote.outcome == "YES":
            if quote.side == "BUY":
                inventory.yes_shares += quote.size
            else:  # SELL
                inventory.yes_shares -= quote.size
                # Spread captured on a round trip (sold higher than we bought)
                if inventory.yes_shares >= 0:
                    spread_earned = quote.size * self.cfg.base_spread_pct / 2
                    inventory.total_spread_captured += spread_earned
                    self.daily_pnl += spread_earned
        elif quote.outcome == "NO":
            if quote.side == "BUY":
                inventory.no_shares += quote.size
            else:
                inventory.no_shares -= quote.size
                if inventory.no_shares >= 0:
                    spread_earned = quote.size * self.cfg.base_spread_pct / 2
                    inventory.total_spread_captured += spread_earned
                    self.daily_pnl += spread_earned

        # Check for adverse selection: if we just got filled and the price
        # immediately moved against us, that's adverse selection
        # (We'll check this in the main loop after the fill)

    def record_adverse_fill(self):
        """Called when a fill immediately moved against us."""
        self.consecutive_adverse += 1
        if self.consecutive_adverse >= self.cfg.max_adverse_fills:
            self.pause_until = time.time() + self.cfg.adverse_fill_pause
            logger.warning(
                f"⚡ ADVERSE SELECTION PAUSE: {self.consecutive_adverse} consecutive "
                f"adverse fills — pausing {self.cfg.adverse_fill_pause}s"
            )
            self.consecutive_adverse = 0

    def record_good_fill(self):
        self.consecutive_adverse = 0


# ═══════════════════════════════════════════════════════════
#  MAIN MARKET MAKER ORCHESTRATOR
# ═══════════════════════════════════════════════════════════

class MarketMaker:
    """
    Main orchestrator that ties everything together.

    The market making loop:
    1. Get fair value estimate for each active market
    2. Generate bid/ask quotes (with inventory skew + volatility spread)
    3. Cancel stale quotes
    4. Post new quotes
    5. Check for fills
    6. Update inventory
    7. Repeat every 500ms

    Think of this like running a currency exchange booth that reprices
    every half-second based on how much inventory you're holding and
    how volatile the market is.
    """

    def __init__(self, cfg: MMConfig, mode: str = "paper"):
        self.cfg = cfg
        self.mode = mode

        # Components
        self.btc_feed = BTCPriceFeed(cfg)
        self.poly_feed = PolymarketFeed(cfg)
        self.fair_value = FairValueEngine()
        self.quote_engine = QuoteEngine(cfg)
        self.risk = MMRiskManager(cfg)
        self.stats = MMStats()

        # Order manager (paper or live)
        if mode == "live" and not cfg.paper_trading:
            self.orders = LiveOrderManager(cfg)
            logger.warning("🔴 LIVE MARKET MAKING — Real money at risk!")
        else:
            self.orders = PaperOrderManager(cfg)
            logger.info("📋 PAPER MARKET MAKING — Simulated fills only")

        self._running = False
        self._start_time = time.time()

    async def start(self):
        self._running = True
        logger.info("=" * 60)
        logger.info("🏪 POLYMARKET MARKET MAKER STARTING")
        logger.info(f"   Mode: {'PAPER' if self.cfg.paper_trading else '🔴 LIVE'}")
        logger.info(f"   Capital: ${self.cfg.initial_capital:.2f}")
        logger.info(f"   Base spread: {self.cfg.base_spread_pct*100:.1f}¢")
        logger.info(f"   Quote size: {self.cfg.quote_size_shares} shares")
        logger.info(f"   Max inventory: {self.cfg.max_inventory_shares} shares")
        logger.info(f"   Quote refresh: {self.cfg.quote_refresh_ms}ms")
        logger.info("=" * 60)

        await asyncio.gather(
            self.btc_feed.connect(),
            self.poly_feed.start(),
            self._quoting_loop(),
            self._status_reporter(),
            self._state_writer(),
        )

    async def _quoting_loop(self):
        """
        Core market making loop — runs every 500ms.

        Each cycle:
        1. Check risk limits
        2. For each quoteable market:
           a. Estimate fair value
           b. Generate bid/ask quotes with inventory skew
           c. Cancel old quotes
           d. Post new quotes
           e. Check for fills and update inventory
        """
        logger.info("⏳ Waiting for price feeds...")
        await asyncio.sleep(5)

        while self._running:
            loop_start = time.perf_counter()

            try:
                # Skip if BTC price not available yet
                if self.btc_feed.price <= 0:
                    await asyncio.sleep(1)
                    continue

                # Check if we're allowed to quote
                can_quote, reason = self.risk.can_quote()
                if not can_quote:
                    # Cancel all outstanding quotes when we can't trade
                    cancelled = self.orders.cancel_all()
                    if cancelled:
                        logger.info(f"🚫 Cannot quote ({reason}) — cancelled {cancelled} orders")
                    await asyncio.sleep(5)
                    continue

                # Get all quoteable markets
                markets = self.poly_feed.get_quoteable_markets(self.cfg)

                for contract in markets:
                    await self._quote_market(contract)

            except Exception as e:
                logger.error(f"Quoting loop error: {e}")

            # Maintain ~500ms cycle
            elapsed_ms = (time.perf_counter() - loop_start) * 1000
            sleep_ms = max(0, self.cfg.quote_refresh_ms - elapsed_ms)
            await asyncio.sleep(sleep_ms / 1000)

    async def _quote_market(self, contract: MarketContract):
        """Generate and post quotes for a single market."""

        # ── Step 1: Check if we should stop quoting this market ──
        if contract.seconds_to_expiry < self.cfg.expiry_cutoff_seconds:
            # Close to expiry — cancel all quotes and let inventory settle
            self.orders.cancel_all(contract.yes_token_id)
            self.orders.cancel_all(contract.no_token_id)
            logger.debug(f"⏰ Expiry cutoff — stopped quoting {contract.question[:40]}")
            return

        # ── Step 2: Get inventory for this market ──
        inventory = self.risk.get_inventory(contract.condition_id)

        # Check inventory limits — if exceeded, only quote the reducing side
        if not self.risk.check_inventory_limit(inventory):
            self.orders.cancel_all(contract.yes_token_id)
            self.orders.cancel_all(contract.no_token_id)
            logger.warning(
                f"📦 Inventory limit! Net: {inventory.net_exposure:+.1f} | "
                f"Cancelling all quotes for {contract.question[:40]}"
            )
            return

        # ── Step 3: Estimate fair value ──
        fv = self.fair_value.estimate(
            self.btc_feed.price,
            self.btc_feed.change_1m,
            self.btc_feed.change_5m,
            contract,
            volume_ratio=self.btc_feed.volume_ratio,
        )

        # ── Step 4: Generate quotes ──
        quotes = self.quote_engine.generate_quotes(
            fair_value=fv,
            inventory=inventory,
            volatility=self.btc_feed.volatility_1m,
            contract=contract,
        )

        # ── Step 5: Cancel existing quotes and post new ones ──
        # In a real HFT system, you'd amend orders instead of cancel+replace.
        # Polymarket CLOB may not support amend, so we cancel and repost.
        self.orders.cancel_all(contract.yes_token_id)
        self.orders.cancel_all(contract.no_token_id)

        # Post YES side bid and ask
        if contract.yes_token_id:
            self.orders.post_quote(
                contract.yes_token_id, "YES", "BUY",
                quotes["yes_bid"], quotes["yes_size"],
            )
            self.orders.post_quote(
                contract.yes_token_id, "YES", "SELL",
                quotes["yes_ask"], quotes["yes_size"],
            )

        # Post NO side bid and ask
        if contract.no_token_id:
            self.orders.post_quote(
                contract.no_token_id, "NO", "BUY",
                quotes["no_bid"], quotes["no_size"],
            )
            self.orders.post_quote(
                contract.no_token_id, "NO", "SELL",
                quotes["no_ask"], quotes["no_size"],
            )

        self.stats.total_quotes_posted += 4

        # ── Step 6: Check for fills ──
        fills = self.orders.check_fills(contract)
        for fill in fills:
            self.stats.total_fills += 1
            self.risk.record_fill(fill, inventory, contract)

            icon = "🟢" if fill.side == "BUY" else "🔴"
            logger.info(
                f"{icon} FILL | {fill.outcome} {fill.side} {fill.size:.0f} shares "
                f"@ ${fill.fill_price:.4f} | "
                f"Net inventory: {inventory.net_exposure:+.1f} | "
                f"Spread earned: ${inventory.total_spread_captured:.4f}"
            )

        # Log quote levels at DEBUG
        logger.debug(
            f"📊 {contract.question[:30]} | FV: {fv:.4f} | "
            f"YES: {quotes['yes_bid']:.4f}/{quotes['yes_ask']:.4f} | "
            f"NO: {quotes['no_bid']:.4f}/{quotes['no_ask']:.4f} | "
            f"Spread: {quotes['effective_spread']*100:.1f}¢ | "
            f"Skew: {quotes['skew']:+.4f} | "
            f"Inv: {inventory.net_exposure:+.1f}"
        )

    async def _status_reporter(self):
        """Print summary every 30 seconds."""
        await asyncio.sleep(10)
        while self._running:
            markets = len(self.poly_feed.get_quoteable_markets(self.cfg))
            active_quotes = len(self.orders.active_quotes)
            total_inv = sum(
                abs(inv.net_exposure) for inv in self.risk.inventories.values()
            )
            uptime = int(time.time() - self._start_time)
            uptime_s = f"{uptime//3600:02d}:{(uptime%3600)//60:02d}:{uptime%60:02d}"

            logger.info(
                f"🏪 MM STATUS | BTC: ${self.btc_feed.price:,.2f} | "
                f"Markets: {markets} | Active quotes: {active_quotes} | "
                f"Fills: {self.stats.total_fills} | "
                f"Spread P&L: ${self.risk.daily_pnl:+.4f} | "
                f"Inventory: {total_inv:.0f} shares | "
                f"Vol: {self.btc_feed.volatility_1m*10000:.2f}bp | "
                f"Up: {uptime_s}"
            )
            await asyncio.sleep(30)

    async def _state_writer(self):
        """Write state to JSON for dashboard consumption."""
        while self._running:
            try:
                inventories = {}
                for cid, inv in self.risk.inventories.items():
                    inventories[cid] = {
                        "yes_shares": inv.yes_shares,
                        "no_shares": inv.no_shares,
                        "net_exposure": inv.net_exposure,
                        "spread_captured": inv.total_spread_captured,
                    }

                state = {
                    "running": self._running,
                    "mode": self.mode,
                    "capital": self.risk.capital,
                    "btc_price": self.btc_feed.price,
                    "btc_volatility": self.btc_feed.volatility_1m,
                    "daily_pnl": self.risk.daily_pnl,
                    "total_fills": self.stats.total_fills,
                    "total_quotes": self.stats.total_quotes_posted,
                    "active_quotes": len(self.orders.active_quotes),
                    "markets_tracked": len(self.poly_feed.markets),
                    "inventories": inventories,
                    "uptime_seconds": int(time.time() - self._start_time),
                    "last_updated": datetime.now(timezone.utc).strftime("%H:%M:%S UTC"),
                }
                Path(self.cfg.data_dir).mkdir(exist_ok=True)
                with open(self.cfg.mm_state_file, "w") as f:
                    json.dump(state, f)
            except Exception:
                pass
            await asyncio.sleep(1)

    def stop(self):
        """Graceful shutdown — cancel all quotes before stopping."""
        logger.info("🛑 Shutting down — cancelling all quotes...")
        cancelled = self.orders.cancel_all()
        logger.info(f"   Cancelled {cancelled} quotes")
        self._running = False
        self.btc_feed.stop()

        # Final summary
        logger.info("=" * 60)
        logger.info("🏪 MARKET MAKER SESSION SUMMARY")
        logger.info(f"   Total quotes posted: {self.stats.total_quotes_posted}")
        logger.info(f"   Total fills: {self.stats.total_fills}")
        logger.info(f"   Spread P&L: ${self.risk.daily_pnl:+.4f}")
        for cid, inv in self.risk.inventories.items():
            logger.info(
                f"   {cid[:8]}: net={inv.net_exposure:+.1f} "
                f"spread=${inv.total_spread_captured:.4f}"
            )
        logger.info("=" * 60)


# ═══════════════════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════════════════

async def main(mode: str):
    cfg = MMConfig()
    setup_logging(cfg)

    mm = MarketMaker(cfg, mode=mode)

    try:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, mm.stop)
    except (NotImplementedError, AttributeError):
        signal.signal(signal.SIGINT, lambda *_: mm.stop())

    try:
        await mm.start()
    except KeyboardInterrupt:
        mm.stop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Polymarket Market Maker Bot")
    parser.add_argument("--mode", choices=["paper", "live"], default="paper",
                        help="Trading mode (default: paper)")
    args = parser.parse_args()

    print(f"""
╔══════════════════════════════════════════════╗
║   POLYMARKET MARKET MAKER v1.0               ║
║   Mode: {'PAPER (safe)         ' if args.mode == 'paper' else '🔴 LIVE TRADING       '}           ║
║   Strategy: Two-sided spread capture         ║
║   Profit: Bid-ask spread (1-4¢ per trip)     ║
╚══════════════════════════════════════════════╝
    """)

    loop = asyncio.SelectorEventLoop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(main(args.mode))
