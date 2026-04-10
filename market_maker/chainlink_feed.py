"""
Chainlink BTC/USD price feed — Polygon mainnet.

This is the SETTLEMENT SOURCE for Polymarket BTC 5-minute markets.
Polymarket resolves YES/NO against this exact price, not Binance.

Why this matters for market making:
  Chainlink updates on-chain when BTC moves >0.5% OR every 27 seconds.
  Binance updates continuously (milliseconds).

  The gap between Binance and Chainlink is a predictive signal:
    Binance > Chainlink → Chainlink will update UP → YES more likely to win
    Binance < Chainlink → Chainlink will update DOWN → NO more likely to win

  This is the most direct edge available for these markets.

Contract: 0xc907E116054Ad103354f2D350FD2514433D57F6f (Polygon Mainnet)
Method:   latestAnswer() → int256 with 8 decimal places
RPC:      Configurable via ALCHEMY_POLYGON_KEY or POLYGON_RPC_URL.
          Falls back to public Polygon RPCs if neither is set.
"""

import asyncio
import time

import aiohttp
from loguru import logger

_CONTRACT = "0xc907E116054Ad103354f2D350FD2514433D57F6f"
_SELECTOR = "0x50d25bcd"   # keccak256("latestAnswer()")[:4]

import os


def _build_rpc_endpoints() -> list[str]:
    """
    Build the Polygon RPC fallback chain.

    Priority order:
      1. POLYGON_RPC_URL     — full URL override (highest priority)
      2. ALCHEMY_POLYGON_KEY — if set, builds the Alchemy Polygon URL
      3. Public fallbacks    — in descending order of reliability
    """
    endpoints: list[str] = []

    custom = os.getenv("POLYGON_RPC_URL", "").strip()
    if custom:
        endpoints.append(custom)

    alchemy_key = os.getenv("ALCHEMY_POLYGON_KEY", "").strip()
    if alchemy_key:
        endpoints.append(
            f"https://polygon-mainnet.g.alchemy.com/v2/{alchemy_key}"
        )

    # Public fallbacks — most-reliable first, polygon-rpc.com last
    # because it rate-limits aggressively and is frequently flaky.
    endpoints += [
        "https://polygon.llamarpc.com",
        "https://rpc.ankr.com/polygon",
        "https://1rpc.io/matic",
        "https://polygon-rpc.com",
    ]

    # De-dupe while preserving order (user may have set POLYGON_RPC_URL
    # to one of the public fallbacks).
    seen: set[str] = set()
    return [e for e in endpoints if not (e in seen or seen.add(e))]


_RPC_ENDPOINTS = _build_rpc_endpoints()


class ChainlinkBTCFeed:
    """
    Polls Chainlink BTC/USD on Polygon every 10 seconds.

    Usage:
        feed = ChainlinkBTCFeed()
        await feed.start()
        print(feed.price)           # latest on-chain price
        print(feed.binance_lead)    # set externally for divergence signal
        await feed.stop()
    """

    def __init__(self, poll_interval: float = 10.0):
        self._poll_interval = poll_interval
        self._price: float = 0.0
        self._last_update: float = 0.0
        self._running = False
        self._task: asyncio.Task | None = None
        self._session: aiohttp.ClientSession | None = None
        # Set this from BinanceBTCFeed each cycle to compute divergence
        self.binance_price: float = 0.0

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def start(self):
        self._running = True
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=8)
        )
        await self._fetch()   # immediate first read
        self._task = asyncio.create_task(self._poll_loop())

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._session is not None:
            await self._session.close()
            self._session = None

    # ── Polling ───────────────────────────────────────────────────────────────

    async def _poll_loop(self):
        while self._running:
            await asyncio.sleep(self._poll_interval)
            await self._fetch()

    async def _fetch(self):
        payload = {
            "jsonrpc": "2.0",
            "method": "eth_call",
            "params": [{"to": _CONTRACT, "data": _SELECTOR}, "latest"],
            "id": 1,
        }
        result = await self._call_rpc(payload)
        if not result or result == "0x":
            return
        try:
            new_price = int(result, 16) / 1e8
            # Sanity check: reject prices outside $1k–$500k range or >20% jump
            if not (1_000 < new_price < 500_000):
                logger.debug(f"Chainlink: rejected out-of-range price ${new_price:,.2f}")
                return
            if self._price > 0 and abs(new_price - self._price) / self._price > 0.20:
                logger.debug(f"Chainlink: rejected spike ${self._price:,.2f} → ${new_price:,.2f}")
                return
            if new_price != self._price and self._price > 0:
                logger.debug(
                    f"Chainlink BTC/USD updated: ${self._price:,.2f} → ${new_price:,.2f}"
                )
            self._price = new_price
            self._last_update = time.time()
        except Exception as e:
            logger.debug(f"Chainlink price parse error: {e}")

    async def _call_rpc(self, payload: dict) -> str:
        """Try each RPC in the fallback chain; return hex result or empty string."""
        if self._session is None:
            # Safety net: if called before start(), lazily create a session.
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=8)
            )
        for rpc_url in _RPC_ENDPOINTS:
            try:
                async with self._session.post(rpc_url, json=payload) as resp:
                    data = await resp.json(content_type=None)
                result = data.get("result", "")
                if result and result != "0x":
                    return result
            except Exception as e:
                logger.debug(f"Chainlink RPC {rpc_url} failed: {e}")
        return ""

    # ── Public properties ─────────────────────────────────────────────────────

    @property
    def price(self) -> float:
        """Latest on-chain Chainlink BTC/USD price."""
        return self._price

    @property
    def age(self) -> float:
        """Seconds since last successful fetch."""
        return time.time() - self._last_update if self._last_update else float("inf")

    @property
    def is_fresh(self) -> bool:
        return self.age < 60.0

    @property
    def binance_lead(self) -> float:
        """
        How far Binance is ahead of Chainlink in USD.
        Positive = Binance higher → Chainlink likely to update UP.
        Negative = Binance lower  → Chainlink likely to update DOWN.
        """
        if not self._price or not self.binance_price:
            return 0.0
        return self.binance_price - self._price

    @property
    def binance_lead_pct(self) -> float:
        """Binance lead as a fraction of Chainlink price."""
        if not self._price:
            return 0.0
        return self.binance_lead / self._price

    def get_snapshot_fields(self) -> dict:
        """Returns dict matching SideDataSnapshot.chainlink_price."""
        return {"chainlink_price": self._price}

    def status(self) -> dict:
        return {
            "chainlink_price": f"${self._price:,.2f}" if self._price else "N/A",
            "binance_price":   f"${self.binance_price:,.2f}" if self.binance_price else "N/A",
            "binance_lead":    f"${self.binance_lead:+.2f}",
            "lead_pct":        f"{self.binance_lead_pct*100:+.4f}%",
            "age":             f"{self.age:.0f}s",
            "fresh":           self.is_fresh,
        }
