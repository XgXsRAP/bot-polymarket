"""
Telegram / Discord alert manager for the market maker bot.

Reads credentials from environment variables:
    TELEGRAM_BOT_TOKEN   — Telegram bot token (from @BotFather)
    TELEGRAM_CHAT_ID     — Chat / channel ID to send to
    DISCORD_WEBHOOK_URL  — Full Discord webhook URL

Both channels are optional: if neither is configured the class is a
no-op so the rest of the code doesn't need conditional guards.

Usage:
    alerter = AlertManager.from_env()
    await alerter.send("Circuit breaker fired")
    await alerter.send("Chainlink stale 120s", key="chainlink_stale", cooldown=600)
"""

import asyncio
import os
import time

import aiohttp
from loguru import logger


class AlertManager:
    """
    Sends alert messages to Telegram and/or Discord.

    send() is fire-and-forget: errors are logged but never raised so that
    a flaky network can't interrupt the trading loop.

    Cooldown support: pass a `key` string and a `cooldown` (seconds) to
    suppress duplicate alerts within that window.
    """

    def __init__(
        self,
        telegram_token: str | None = None,
        telegram_chat_id: str | None = None,
        discord_webhook: str | None = None,
    ):
        self._tg_token = telegram_token or ""
        self._tg_chat = telegram_chat_id or ""
        self._discord_url = discord_webhook or ""
        self._last_sent: dict[str, float] = {}  # key → last send timestamp

    @classmethod
    def from_env(cls) -> "AlertManager":
        return cls(
            telegram_token=os.environ.get("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID"),
            discord_webhook=os.environ.get("DISCORD_WEBHOOK_URL"),
        )

    @property
    def enabled(self) -> bool:
        return bool((self._tg_token and self._tg_chat) or self._discord_url)

    def _on_cooldown(self, key: str, cooldown: float) -> bool:
        if not key:
            return False
        last = self._last_sent.get(key, 0.0)
        return (time.time() - last) < cooldown

    def _mark_sent(self, key: str) -> None:
        if key:
            self._last_sent[key] = time.time()

    async def send(
        self,
        message: str,
        key: str = "",
        cooldown: float = 0.0,
    ) -> None:
        """
        Send `message` to all configured channels.

        Args:
            message:  The text to send.
            key:      Deduplication key (e.g. "circuit_breaker").
            cooldown: Minimum seconds between sends for this key (0 = no limit).
        """
        if not self.enabled:
            return
        if cooldown > 0 and self._on_cooldown(key, cooldown):
            return

        self._mark_sent(key)
        tasks = []
        if self._tg_token and self._tg_chat:
            tasks.append(self._send_telegram(message))
        if self._discord_url:
            tasks.append(self._send_discord(message))

        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, Exception):
                logger.debug(f"AlertManager: delivery error — {r}")

    async def _send_telegram(self, message: str) -> None:
        url = f"https://api.telegram.org/bot{self._tg_token}/sendMessage"
        payload = {"chat_id": self._tg_chat, "text": message, "parse_mode": "HTML"}
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, json=payload, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise RuntimeError(f"Telegram HTTP {resp.status}: {body[:120]}")

    async def _send_discord(self, message: str) -> None:
        payload = {"content": message}
        async with aiohttp.ClientSession() as session:
            async with session.post(
                self._discord_url,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status not in (200, 204):
                    body = await resp.text()
                    raise RuntimeError(f"Discord HTTP {resp.status}: {body[:120]}")
