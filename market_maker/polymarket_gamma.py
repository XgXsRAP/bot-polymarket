"""
Polymarket Gamma API feed for active BTC 5-minute markets.

Uses the deterministic slug approach from bot.py:
  Every BTC 5-min market has slug: btc-updown-5m-{window_start_timestamp}
  where window_start = now - (now % 300), always divisible by 300.

  We CALCULATE the slug from the clock and query GET /events?slug=<slug>
  directly — one targeted call, zero scanning, zero regex.

Provides:
  - market_spread:     Current YES bid-ask spread (probability units)
  - seconds_to_expiry: Time until market closes
  - best_bid / best_ask: Live order book top of book
"""

import asyncio
import time
from datetime import datetime, timezone

import aiohttp
from loguru import logger

_GAMMA_BASE = "https://gamma-api.polymarket.com"
_WINDOW = 300  # 5-minute markets


def _current_slug(offset: int = 0) -> str:
    """
    Calculate the deterministic slug for a BTC 5-min market window.

    offset=0  → current window
    offset=-1 → previous window (fallback if current isn't live yet)
    offset=1  → next window (check ahead)
    """
    now = int(time.time())
    window_start = now - (now % _WINDOW) + (offset * _WINDOW)
    return f"btc-updown-5m-{window_start}"


class PolymarketGammaFeed:
    """
    Polls the Polymarket Gamma API using deterministic slugs.

    Polls every 10 seconds. On each poll:
      1. Calculate slug for the current 5-min window
      2. If slug changed (new window started), fetch fresh market data
      3. Update bid/ask/expiry for the quote engine
    """

    def __init__(self, poll_interval: float = 10.0):
        self._poll_interval = poll_interval
        self._running = False
        self._task: asyncio.Task | None = None

        # Cached market state
        self._condition_id: str | None = None
        self._best_bid: float = 0.0
        self._best_ask: float = 1.0
        self._end_date_iso: str | None = None
        self._last_update: float = 0.0
        self._last_slug: str = ""

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def start(self):
        self._running = True
        await self._fetch_current()
        self._task = asyncio.create_task(self._poll_loop())

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    # ── Polling ───────────────────────────────────────────────────────────────

    async def _poll_loop(self):
        while self._running:
            await asyncio.sleep(self._poll_interval)
            await self._fetch_current()

    async def _fetch_current(self):
        """Fetch the current 5-min window market using its deterministic slug."""
        # Try current window, fall back to previous if not live yet
        for offset in [0, -1]:
            slug = _current_slug(offset)
            if slug == self._last_slug and offset == 0:
                # Same window, data is still valid — skip re-fetch unless stale
                if time.time() - self._last_update < 30:
                    return
            market = await self._fetch_by_slug(slug)
            if market:
                self._parse(market, slug)
                return

        logger.debug("Gamma: no active 5-min BTC market found for current window")

    async def _fetch_by_slug(self, slug: str) -> dict | None:
        """Query GET /events?slug=<slug> and return the first market inside."""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{_GAMMA_BASE}/events",
                    params={"slug": slug},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status != 200:
                        logger.debug(f"Gamma slug {slug}: HTTP {resp.status}")
                        return None
                    events = await resp.json()

            if not events:
                return None

            event = events[0]
            markets = event.get("markets") or []
            if not markets:
                return None

            # Attach the event-level endDate to the market dict
            market = markets[0]
            market["_event_endDate"] = event.get("endDate") or event.get("endDateIso") or ""
            market["_event_title"] = event.get("title") or ""
            return market

        except Exception as e:
            logger.debug(f"Gamma fetch error for slug {slug}: {e}")
            return None

    # ── Parsing ───────────────────────────────────────────────────────────────

    def _parse(self, market: dict, slug: str):
        self._condition_id = market.get("conditionId") or market.get("id")
        self._last_slug = slug

        # Use event-level endDate (most reliable)
        self._end_date_iso = (
            market.get("_event_endDate")
            or market.get("endDate")
            or market.get("endDateIso")
            or ""
        )

        # Bid/ask — direct fields are most accurate
        if market.get("bestBid") is not None and market.get("bestAsk") is not None:
            self._best_bid = float(market["bestBid"])
            self._best_ask = float(market["bestAsk"])
        else:
            # Fall back to outcomePrices (mid-price) ± half spread
            prices = market.get("outcomePrices") or []
            raw_spread = float(market.get("spread") or 0.01)
            if prices:
                mid = float(prices[0])
                self._best_bid = max(0.01, mid - raw_spread / 2)
                self._best_ask = min(0.99, mid + raw_spread / 2)

        self._last_update = time.time()
        logger.debug(
            f"Gamma: {market.get('_event_title',slug)} | "
            f"expiry={self.seconds_to_expiry:.0f}s | "
            f"bid={self._best_bid:.4f} ask={self._best_ask:.4f} "
            f"spread={self.market_spread:.4f}"
        )

    # ── Public properties ─────────────────────────────────────────────────────

    @property
    def seconds_to_expiry(self) -> float:
        if not self._end_date_iso:
            return 300.0
        try:
            end_dt = datetime.fromisoformat(self._end_date_iso.replace("Z", "+00:00"))
            remaining = (end_dt - datetime.now(timezone.utc)).total_seconds()
            return max(0.0, remaining)
        except Exception:
            return 300.0

    @property
    def market_spread(self) -> float:
        return max(0.0, min(self._best_ask - self._best_bid, 1.0))

    @property
    def best_bid(self) -> float:
        return self._best_bid

    @property
    def best_ask(self) -> float:
        return self._best_ask

    @property
    def is_fresh(self) -> bool:
        return (time.time() - self._last_update) < 30.0

    def get_snapshot_fields(self) -> dict:
        return {
            "market_spread": self.market_spread,
            "seconds_to_expiry": self.seconds_to_expiry,
        }

    def status(self) -> dict:
        return {
            "condition_id": (self._condition_id or "N/A")[:20],
            "slug": self._last_slug,
            "best_bid": f"{self._best_bid:.4f}",
            "best_ask": f"{self._best_ask:.4f}",
            "spread": f"{self.market_spread:.4f}",
            "seconds_to_expiry": f"{self.seconds_to_expiry:.0f}s",
            "fresh": self.is_fresh,
        }
