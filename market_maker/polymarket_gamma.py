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
import json
import time
from datetime import datetime, timezone

import aiohttp
from loguru import logger

try:
    import websockets
    HAS_WEBSOCKETS = True
except ImportError:
    HAS_WEBSOCKETS = False

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
    Polls the Polymarket Gamma API using deterministic slugs, and supplements
    bid/ask with a CLOB WebSocket subscription for sub-second book updates.

    Polls every 10 seconds (REST). On each poll:
      1. Calculate slug for the current 5-min window
      2. If slug changed (new window started), fetch fresh market data
      3. Update bid/ask/expiry for the quote engine

    CLOB WebSocket subscribes to channel "book" for the active condition_id
    and overwrites _best_bid/_best_ask on every book event (~sub-second).
    REST poll remains active as a correction layer (e.g. after top-of-book
    removals that the incremental WS path may miss).
    """

    CLOB_WS = "wss://clob.polymarket.com/ws/"

    def __init__(self, poll_interval: float = 10.0):
        self._poll_interval = poll_interval
        self._running = False
        self._task: asyncio.Task | None = None

        # Cached market state
        self._condition_id: str | None = None
        self._market_id: str | None = None   # numeric Gamma market id for fast polling
        self._best_bid: float = 0.0
        self._best_ask: float = 1.0
        self._end_date_iso: str | None = None
        self._last_update: float = 0.0
        self._last_slug: str = ""
        self._yes_token_id: str | None = None
        self._no_token_id: str | None = None

        # Persistent HTTP session (created in start(), closed in stop())
        self._session: aiohttp.ClientSession | None = None

        # Fallback tracking
        self._slug_miss_count: int = 0
        self._using_cached_market: bool = False

        # CLOB WebSocket state
        self._ws_task: asyncio.Task | None = None
        self._subscribed_cid: str | None = None  # cid currently subscribed on the WS

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def start(self):
        self._running = True
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10)
        )
        await self._fetch_current()
        self._task = asyncio.create_task(self._poll_loop())
        # Always start the CLOB REST book poller (2s updates, no auth needed)
        asyncio.create_task(self._clob_book_loop(), name="clob_book_rest")
        if HAS_WEBSOCKETS:
            self._ws_task = asyncio.create_task(
                self._ws_book_listener(), name="clob_book_ws"
            )
        else:
            logger.warning("PolymarketGamma: websockets not installed, using REST-only bid/ask")

    async def stop(self):
        self._running = False
        if self._ws_task and not self._ws_task.done():
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    # ── Polling ───────────────────────────────────────────────────────────────

    async def _poll_loop(self):
        while self._running:
            await asyncio.sleep(self._poll_interval)
            await self._fetch_current()

    async def _clob_book_loop(self):
        """
        Fast Gamma price poll every 3 seconds using the prices endpoint.
        Updates best_bid/best_ask from outcomePrices + spread without
        waiting for the full 10-second REST poll cycle.
        """
        while self._running:
            await asyncio.sleep(3)
            if not self._market_id:
                continue
            try:
                session = self._session or aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=5)
                )
                # GET /markets/{numeric_id} — fastest per-market price endpoint
                async with session.get(
                    f"{_GAMMA_BASE}/markets/{self._market_id}",
                ) as resp:
                    if resp.status != 200:
                        continue
                    data = await resp.json()
                    best_bid = data.get("bestBid")
                    best_ask = data.get("bestAsk")
                    prices   = data.get("outcomePrices") or []
                    spread   = float(data.get("spread") or 0.01)

                    if best_bid is not None and best_ask is not None:
                        self._best_bid = float(best_bid)
                        self._best_ask = float(best_ask)
                        self._last_update = time.time()
                    elif prices:
                        mid = float(prices[0])
                        self._best_bid = max(0.01, mid - spread / 2)
                        self._best_ask = min(0.99, mid + spread / 2)
                        self._last_update = time.time()
            except Exception as e:
                logger.debug(f"Fast price poll error: {e}")

    # ── CLOB WebSocket ─────────────────────────────────────────────────────────

    async def _ws_book_listener(self) -> None:
        """
        Maintain a persistent CLOB WebSocket connection and subscribe to the
        'book' channel for the active condition_id.  Updates _best_bid and
        _best_ask sub-second from book snapshots and price_change events.
        Resubscribes automatically when the condition_id rotates (new 5-min window).
        Reconnects with 5-second backoff on any error.
        """
        while self._running:
            try:
                async with websockets.connect(
                    self.CLOB_WS, ping_interval=20, ping_timeout=10
                ) as ws:
                    self._subscribed_cid = None
                    logger.info("PolymarketGamma WS: connected to CLOB book channel")
                    while self._running:
                        cid = self._condition_id
                        if cid and cid != self._subscribed_cid:
                            await ws.send(json.dumps({
                                "type": "subscribe",
                                "channel": "book",
                                "market": cid,
                            }))
                            self._subscribed_cid = cid
                            logger.debug(f"PolymarketGamma WS: subscribed to book for {cid[:16]}…")
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                            self._handle_book_msg(raw)
                        except asyncio.TimeoutError:
                            pass  # no message yet; loop to check for cid rotation
            except asyncio.CancelledError:
                return
            except Exception as exc:
                self._subscribed_cid = None
                exc_str = str(exc)
                # HTTP 404/401/403 = endpoint unavailable without auth credentials.
                # No point retrying — fall back to REST-only bid/ask (10s polls).
                if any(f"HTTP {c}" in exc_str for c in ("404", "401", "403")):
                    logger.info(
                        "PolymarketGamma WS: book channel unavailable without CLOB "
                        "credentials (HTTP error) — using REST-only bid/ask updates."
                    )
                    return
                logger.warning(
                    f"PolymarketGamma WS: disconnected ({exc}); reconnecting in 5s"
                )
                await asyncio.sleep(5)

    def _handle_book_msg(self, raw: str) -> None:
        """Parse a CLOB WebSocket message and update top-of-book bid/ask."""
        try:
            msgs = json.loads(raw)
            if not isinstance(msgs, list):
                msgs = [msgs]
            for msg in msgs:
                etype = msg.get("event_type")
                if etype == "book":
                    # Full book snapshot: buys sorted desc, sells sorted asc
                    buys = msg.get("buys") or []
                    sells = msg.get("sells") or []
                    if buys:
                        self._best_bid = float(buys[0]["price"])
                    if sells:
                        self._best_ask = float(sells[0]["price"])
                    if buys or sells:
                        self._last_update = time.time()
                elif etype == "price_change":
                    # Incremental update: raise top bid or lower top ask on new levels;
                    # removals (size==0) of the current top are left for REST poll to correct
                    for change in msg.get("changes") or []:
                        side = change.get("side", "").lower()
                        price = float(change.get("price", 0))
                        size = float(change.get("size", 0))
                        if size == 0:
                            continue  # level removed — REST poll will correct within 10s
                        if side == "buy" and price > self._best_bid:
                            self._best_bid = price
                            self._last_update = time.time()
                        elif side == "sell" and (self._best_ask <= 0 or price < self._best_ask):
                            self._best_ask = price
                            self._last_update = time.time()
        except Exception as exc:
            logger.debug(f"PolymarketGamma WS parse error: {exc}")

    async def _fetch_current(self):
        """Fetch the current 5-min window market using its deterministic slug."""
        # Try current window → next (pre-published) → previous (just ended)
        for offset in [0, 1, -1]:
            slug = _current_slug(offset)
            market = await self._fetch_by_slug(slug)
            if market:
                self._slug_miss_count = 0
                self._parse(market, slug)
                return

        # All slug offsets missed
        self._slug_miss_count += 1

        # After 3 consecutive misses, try tag-based search as fallback
        if self._slug_miss_count >= 3:
            market = await self._fetch_by_tag_search()
            if market:
                self._slug_miss_count = 0
                self._parse(market, slug=f"tag-fallback-{int(time.time())}")
                return

        # Log only once when we first fall back to cached data
        if self._condition_id and not self._using_cached_market:
            self._using_cached_market = True
            logger.debug(
                f"Gamma: using cached market {(self._condition_id or '')[:16]}… "
                f"(slug miss #{self._slug_miss_count})"
            )
        elif not self._condition_id:
            logger.debug("Gamma: no active 5-min BTC market found for current window")

    async def _fetch_by_slug(self, slug: str) -> dict | None:
        """Query GET /events?slug=<slug> and return the first market inside."""
        try:
            session = self._session or aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10)
            )
            async with session.get(
                f"{_GAMMA_BASE}/events",
                params={"slug": slug},
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

    async def _fetch_by_tag_search(self) -> dict | None:
        """
        Fallback: query GET /events?tag=btc&closed=false&limit=5
        and find any 5-minute BTC UP/DOWN market by title keyword.
        Used after 3 consecutive slug misses.
        """
        try:
            session = self._session or aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10)
            )
            async with session.get(
                f"{_GAMMA_BASE}/events",
                params={"tag": "btc", "closed": "false", "limit": "5"},
            ) as resp:
                if resp.status != 200:
                    return None
                events = await resp.json()

            for event in (events or []):
                title = (event.get("title") or event.get("slug") or "").lower()
                if not any(kw in title for kw in ("5m", "5-min", "updown-5m", "5 min")):
                    continue
                markets = event.get("markets") or []
                if not markets:
                    continue
                market = markets[0]
                market["_event_endDate"] = event.get("endDate") or event.get("endDateIso") or ""
                market["_event_title"] = event.get("title") or ""
                logger.info(f"Gamma tag-fallback: found market '{market['_event_title']}'")
                return market

        except Exception as e:
            logger.debug(f"Gamma tag-fallback error: {e}")
        return None

    # ── Parsing ───────────────────────────────────────────────────────────────

    def _parse(self, market: dict, slug: str):
        self._using_cached_market = False
        self._condition_id = market.get("conditionId") or market.get("id")
        self._market_id = str(market.get("id") or "")   # numeric id for fast REST polling
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

        # Extract token IDs — Gamma API uses clobTokenIds list aligned with outcomes[]
        # e.g. outcomes=["Up","Down"] → clobTokenIds[0]=YES token, clobTokenIds[1]=NO token
        clob_ids = market.get("clobTokenIds") or []
        outcomes  = market.get("outcomes") or []
        for i, tid in enumerate(clob_ids):
            if not tid:
                continue
            label = outcomes[i].lower() if i < len(outcomes) else ""
            if label in ("up", "yes", "1") or i == 0:
                self._yes_token_id = tid
            elif label in ("down", "no", "0") or i == 1:
                self._no_token_id = tid

        # Legacy fallback: old tokens array format
        for token in market.get("tokens", []):
            outcome = token.get("outcome", "").lower()
            tid = token.get("token_id")
            if outcome in ("yes", "up", "1") and tid:
                self._yes_token_id = tid
            elif outcome in ("no", "down", "0") and tid:
                self._no_token_id = tid

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
    def yes_token_id(self) -> str | None:
        return self._yes_token_id

    @property
    def no_token_id(self) -> str | None:
        return self._no_token_id

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
            "cached": self._using_cached_market,
            "slug_misses": self._slug_miss_count,
        }
