"""
Live order placement on Polymarket's CLOB.

Replaces PaperTrader for real-funds trading. Interface is intentionally
compatible: process_cycle() returns list[Fill] so mm_enhanced1.py's loop
needs no structural changes beyond awaiting the call.

Order lifecycle per cycle:
  1. Cancel all tracked open quote IDs (batch cancel via py_clob_client)
  2. If confidence tier is PAUSED, skip posting and return []
  3. If seconds_to_expiry < 60, skip posting (orders would expire unfilled)
  4. If yes_token_id is None, skip posting (market not yet resolved from Gamma)
  5. POST YES bid  — BUY  yes_token_id @ quotes["yes_bid"]
  6. POST YES ask  — SELL yes_token_id @ quotes["yes_ask"]
  7. Return fills detected since last cycle

Fill detection (dual mechanism with deduplication):
  Primary:  CLOB WebSocket  wss://clob.polymarket.com/ws/user
            → real-time fill events; lowest latency
  Fallback: REST poll       client.get_open_orders()
            → any tracked order_id missing from open set was filled
  Dedup:    _seen_fill_ids prevents the same fill being reported twice
            (pruned after 60 seconds)

Soft dependency: py_clob_client and websockets must be installed.
If either is absent the class still loads but process_cycle() always
returns [] and logs a one-time warning.

Required env vars (read by caller, passed to __init__):
  POLYMARKET_PRIVATE_KEY      — hex wallet private key (0x…)
  POLYMARKET_API_KEY          — CLOB API key
  POLYMARKET_API_SECRET       — CLOB API secret
  POLYMARKET_API_PASSPHRASE   — CLOB API passphrase
"""

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Optional

from loguru import logger

# ── Optional dependency: py_clob_client ──────────────────────────────────────

try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import OrderArgs, BUY, SELL
    CLOB_AVAILABLE = True
except ImportError:
    CLOB_AVAILABLE = False
    logger.warning(
        "py-clob-client not installed — LiveOrderManager will be inert. "
        "Install with: pip install py-clob-client eth-account"
    )

# ── Optional dependency: websockets ──────────────────────────────────────────

try:
    import websockets
    HAS_WEBSOCKETS = True
except ImportError:
    HAS_WEBSOCKETS = False
    logger.warning(
        "websockets not installed — WS fill listener disabled. "
        "Install with: pip install websockets"
    )

# ── Reuse Fill dataclass from paper_trader ───────────────────────────────────

from paper_trader import Fill


# ── Internal order tracking ───────────────────────────────────────────────────

@dataclass
class _TrackedOrder:
    order_id: str
    side: str       # "BUY" or "SELL"
    price: float
    size: float
    token_id: str
    market_id: str
    posted_at: float


# ── LiveOrderManager ──────────────────────────────────────────────────────────

class LiveOrderManager:
    """
    Manages real limit orders on Polymarket's CLOB for market making.

    Usage (mirrors PaperTrader):
        manager = LiveOrderManager(private_key, api_key, secret, passphrase)
        await manager.start()           # starts WS fill listener

        # Each cycle:
        fills = await manager.process_cycle(quotes, snapshot, confidence,
                                            market_id, yes_token_id)
        # fills is list[Fill] — same type as PaperTrader returns

        await manager.stop()            # cancels open orders, closes WS
    """

    CLOB_HOST = "https://clob.polymarket.com"
    CLOB_WS   = "wss://clob.polymarket.com/ws/user"
    CHAIN_ID  = 137   # Polygon Mainnet

    # Orders closer than this to expiry will not be posted
    MIN_SECONDS_TO_EXPIRY = 60

    def __init__(
        self,
        private_key: str,
        api_key: str,
        api_secret: str,
        api_passphrase: str,
    ):
        self._api_key = api_key
        self._api_secret = api_secret
        self._api_passphrase = api_passphrase

        self._client: Optional["ClobClient"] = None

        # Tracked open quotes: order_id → _TrackedOrder
        self._open_quotes: dict[str, _TrackedOrder] = {}

        # Dedup: WS-detected fill order_ids → timestamp seen
        # REST poll skips these so the same fill isn't reported twice.
        self._seen_fill_ids: dict[str, float] = {}

        # Fills queued by WS listener, drained by process_cycle()
        self._pending_fills: list[Fill] = []
        self._fills_lock = asyncio.Lock()

        self._ws_task: Optional[asyncio.Task] = None
        self._ws_connected = False

        if CLOB_AVAILABLE and private_key:
            try:
                self._client = ClobClient(
                    host=self.CLOB_HOST,
                    chain_id=self.CHAIN_ID,
                    key=private_key,
                    creds={
                        "apiKey": api_key,
                        "secret": api_secret,
                        "passphrase": api_passphrase,
                    },
                )
                logger.success("LiveOrderManager: CLOB client initialised (Polygon 137)")
            except Exception as exc:
                logger.error(f"LiveOrderManager: CLOB client init failed: {exc}")
        elif not CLOB_AVAILABLE:
            pass  # already warned at import
        else:
            logger.error("LiveOrderManager: POLYMARKET_PRIVATE_KEY is empty — cannot place orders")

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the background WebSocket fill listener."""
        if HAS_WEBSOCKETS and self._client:
            self._ws_task = asyncio.create_task(
                self._ws_fill_listener(), name="clob_ws_fill_listener"
            )
            logger.info("LiveOrderManager: WS fill listener started")
        else:
            logger.info("LiveOrderManager: running without WS (REST-only fill detection)")

    async def stop(self) -> None:
        """Cancel all open orders then tear down the WS connection."""
        logger.info("LiveOrderManager: shutting down — cancelling open orders")
        await self.cancel_all()
        if self._ws_task and not self._ws_task.done():
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass
        logger.info("LiveOrderManager: stopped")

    # ── Main cycle method ─────────────────────────────────────────────────────

    async def process_cycle(
        self,
        quotes: dict,
        snapshot,               # SideDataSnapshot
        confidence,             # ConfidenceResult
        market_id: str,
        yes_token_id: Optional[str],
    ) -> list[Fill]:
        """
        Cancel stale quotes, post fresh bid+ask, return any fills since
        the last call.

        Compatible with PaperTrader.process_cycle() except this is async.
        """
        if not self._client:
            return []

        # Always drain WS fills first (they arrived asynchronously)
        fills = await self._drain_pending_fills()

        # Prune dedup set entries older than 60 s
        self._prune_seen_fills()

        # Guard: PAUSED — cancel everything and sit out
        if confidence.tier == "PAUSED":
            await self._cancel_open_quotes()
            return fills

        # Guard: too close to expiry — don't post orders that will expire
        if snapshot.seconds_to_expiry < self.MIN_SECONDS_TO_EXPIRY:
            await self._cancel_open_quotes()
            return fills

        # Guard: no token ID yet (Gamma hasn't fetched the market)
        if not yes_token_id:
            logger.debug("LiveOrderManager: yes_token_id not yet available, skipping cycle")
            return fills

        size = quotes.get("size", 0.0) * confidence.size_multiplier
        if size < 1.0:
            await self._cancel_open_quotes()
            return fills

        yes_bid = quotes.get("yes_bid", 0.0)
        yes_ask = quotes.get("yes_ask", 1.0)

        # Sanity: prices must be in (0, 1) exclusive and bid < ask
        if not (0.01 <= yes_bid < yes_ask <= 0.99):
            logger.warning(
                f"LiveOrderManager: invalid quote prices bid={yes_bid:.4f} "
                f"ask={yes_ask:.4f} — skipping"
            )
            await self._cancel_open_quotes()
            return fills

        # Cancel → Replace
        await self._cancel_open_quotes()
        await self._post_order(yes_token_id, yes_bid, size, "BUY",  market_id)
        await self._post_order(yes_token_id, yes_ask, size, "SELL", market_id)

        # REST fallback fill detection (covers WS gaps)
        rest_fills = await self._poll_fills_rest()
        fills.extend(rest_fills)

        return fills

    # ── Order placement ───────────────────────────────────────────────────────

    async def _post_order(
        self,
        token_id: str,
        price: float,
        size: float,
        side: str,          # "BUY" or "SELL"
        market_id: str,
    ) -> Optional[str]:
        """Post a single limit order. Returns order_id or None on failure."""
        if not CLOB_AVAILABLE:
            return None

        clob_side = BUY if side == "BUY" else SELL
        order_args = OrderArgs(
            token_id=token_id,
            price=round(price, 4),
            size=round(size, 2),
            side=clob_side,
        )

        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None, self._client.create_and_post_order, order_args
            )
            order_id = result.get("orderID") or result.get("order_id") or ""
            if not order_id:
                logger.warning(f"LiveOrderManager: POST {side} returned no orderID: {result}")
                return None

            self._open_quotes[order_id] = _TrackedOrder(
                order_id=order_id,
                side=side,
                price=price,
                size=size,
                token_id=token_id,
                market_id=market_id,
                posted_at=time.time(),
            )
            logger.debug(
                f"LiveOrderManager: posted {side} {size:.1f}sh @ {price:.4f} "
                f"id={order_id[:12]}…"
            )
            return order_id

        except Exception as exc:
            logger.error(f"LiveOrderManager: _post_order {side} failed: {exc}")
            return None

    # ── Cancellation ──────────────────────────────────────────────────────────

    async def _cancel_open_quotes(self) -> None:
        """Batch-cancel all currently tracked open orders."""
        if not self._open_quotes or not self._client:
            return

        order_ids = list(self._open_quotes.keys())
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._client.cancel_orders, order_ids)
            logger.debug(f"LiveOrderManager: cancelled {len(order_ids)} quote(s)")
        except Exception as exc:
            logger.warning(f"LiveOrderManager: cancel_orders failed: {exc}")
        finally:
            # Clear tracked orders regardless — next cycle posts fresh quotes
            self._open_quotes.clear()

    async def cancel_all(self) -> None:
        """Public alias used on shutdown."""
        await self._cancel_open_quotes()

    # ── REST fill detection ───────────────────────────────────────────────────

    async def _poll_fills_rest(self) -> list[Fill]:
        """
        Compare tracked order IDs against GET /orders response.
        Any ID that's gone from the open set was filled (or cancelled after
        our own cancel_open_quotes ran — but we only call this AFTER posting
        the new quotes, so leftover IDs are from the just-completed cycle
        and should no longer be tracked).

        We conservatively treat any disappeared tracked order as filled.
        In the worst case (race between cancel and fill) we may over-count
        by one fill per cycle; the WS dedup prevents true double-counts for
        WS-detected fills.
        """
        if not self._client or not self._open_quotes:
            return []

        try:
            loop = asyncio.get_event_loop()
            open_orders = await loop.run_in_executor(
                None, self._client.get_open_orders
            )
            open_ids: set[str] = {
                o.get("id") or o.get("orderID") or ""
                for o in (open_orders or [])
            }
        except Exception as exc:
            logger.warning(f"LiveOrderManager: get_open_orders failed: {exc}")
            return []

        fills: list[Fill] = []
        now = time.time()

        for order_id, tracked in list(self._open_quotes.items()):
            if order_id in open_ids:
                continue  # still resting, not filled
            if order_id in self._seen_fill_ids:
                continue  # already reported via WS
            # Treat as filled
            fill = Fill(
                timestamp=now,
                side="buy_yes" if tracked.side == "BUY" else "sell_yes",
                price=tracked.price,
                size=tracked.size,
                pnl=0.0,
                market_id=tracked.market_id,
            )
            fills.append(fill)
            self._seen_fill_ids[order_id] = now
            del self._open_quotes[order_id]
            logger.info(
                f"LiveOrderManager [REST]: fill detected {fill.side} "
                f"{fill.size:.1f}sh @ {fill.price:.4f}"
            )

        return fills

    # ── WebSocket fill listener ───────────────────────────────────────────────

    async def _ws_fill_listener(self) -> None:
        """
        Subscribe to the CLOB user WebSocket for real-time fill events.
        Reconnects automatically on disconnect.
        """
        while True:
            try:
                async with websockets.connect(
                    self.CLOB_WS,
                    ping_interval=20,
                    ping_timeout=10,
                ) as ws:
                    # Authenticate
                    auth_msg = json.dumps({
                        "auth": {
                            "apiKey": self._api_key,
                            "secret": self._api_secret,
                            "passphrase": self._api_passphrase,
                        }
                    })
                    await ws.send(auth_msg)
                    self._ws_connected = True
                    logger.info("LiveOrderManager: CLOB WS connected")

                    async for raw in ws:
                        await self._handle_ws_message(raw)

            except asyncio.CancelledError:
                self._ws_connected = False
                return
            except Exception as exc:
                self._ws_connected = False
                logger.warning(
                    f"LiveOrderManager: CLOB WS disconnected ({exc}); "
                    "reconnecting in 5 s"
                )
                await asyncio.sleep(5)

    async def _handle_ws_message(self, raw: str) -> None:
        """Parse a raw WS message and queue fill events."""
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return

        # Polymarket WS sends either a single dict or a list
        events = msg if isinstance(msg, list) else [msg]

        now = time.time()
        for event in events:
            if not isinstance(event, dict):
                continue
            if event.get("type") != "fill":
                continue

            data = event.get("data") or event
            order_id = data.get("orderID") or data.get("order_id") or ""
            if not order_id:
                continue

            # Look up the tracked order for context
            tracked = self._open_quotes.get(order_id)

            fill_price = float(data.get("price") or (tracked.price if tracked else 0.0))
            fill_size  = float(data.get("size")  or (tracked.size  if tracked else 0.0))
            ws_side    = (data.get("side") or "BUY").upper()
            market_id  = (tracked.market_id if tracked else "")

            fill = Fill(
                timestamp=now,
                side="buy_yes" if ws_side == "BUY" else "sell_yes",
                price=fill_price,
                size=fill_size,
                pnl=0.0,
                market_id=market_id,
            )

            async with self._fills_lock:
                self._pending_fills.append(fill)

            self._seen_fill_ids[order_id] = now

            if tracked:
                del self._open_quotes[order_id]

            logger.info(
                f"LiveOrderManager [WS]: fill {fill.side} "
                f"{fill.size:.1f}sh @ {fill.price:.4f}"
            )

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _drain_pending_fills(self) -> list[Fill]:
        """Return and clear all fills accumulated by the WS listener."""
        async with self._fills_lock:
            fills = list(self._pending_fills)
            self._pending_fills.clear()
        return fills

    def _prune_seen_fills(self) -> None:
        """Remove dedup entries older than 60 s to prevent unbounded growth."""
        cutoff = time.time() - 60.0
        stale = [k for k, t in self._seen_fill_ids.items() if t < cutoff]
        for k in stale:
            del self._seen_fill_ids[k]

    # ── Status ────────────────────────────────────────────────────────────────

    @property
    def is_connected(self) -> bool:
        return self._ws_connected

    def status(self) -> dict:
        return {
            "clob_available": CLOB_AVAILABLE,
            "client_ready": self._client is not None,
            "ws_connected": self._ws_connected,
            "open_quotes": len(self._open_quotes),
            "pending_fills": len(self._pending_fills),
        }
