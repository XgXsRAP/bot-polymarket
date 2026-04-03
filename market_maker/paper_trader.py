"""
Paper trading execution engine for the market maker.

Simulates order fills against real live Polymarket prices without
touching real funds. Tracks inventory, P&L, and fill history.

Fill logic:
  - A YES bid fills when the live market ask drops to or below our bid
  - A YES ask fills when the live market bid rises to or above our ask
  - Fill probability adds a realistic 15% adverse-selection filter
    (not every crossing results in a fill — queue position matters)

State persists to data/paper_mm_state.json every cycle so the
dashboard can read it without touching the trading loop.
"""

import json
import math
import random
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

from loguru import logger

from fees import net_fill_fee


# ── Data structures ───────────────────────────────────────────────────────────

@dataclass
class Fill:
    timestamp: float
    side: str           # "buy_yes" | "sell_yes"
    price: float
    size: float
    pnl: float          # 0 until position closes
    market_id: str


@dataclass
class PaperState:
    # Capital tracking
    starting_capital: float = 1000.0
    cash: float = 1000.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0

    # Inventory (net YES shares held, negative = short YES = long NO)
    net_inventory: float = 0.0
    avg_entry_price: float = 0.0

    # Session stats
    total_fills: int = 0
    round_trips: int = 0
    winning_trips: int = 0

    # Risk tracking
    session_start: float = field(default_factory=time.time)
    peak_capital: float = 1000.0
    max_drawdown: float = 0.0
    consecutive_losses: int = 0

    # Recent fills (last 100)
    recent_fills: list = field(default_factory=list)

    # Current quotes (for dashboard display)
    current_yes_bid: float = 0.0
    current_yes_ask: float = 1.0
    current_fair_value: float = 0.5
    current_confidence: float = 0.0
    current_spread: float = 0.0
    market_best_bid: float = 0.0
    market_best_ask: float = 1.0

    # Feed status
    last_update: float = 0.0
    market_id: str = ""
    seconds_to_expiry: float = 300.0


# ── Paper Trader ──────────────────────────────────────────────────────────────

class PaperTrader:
    """
    Simulates market-making fills against live Polymarket prices.

    Usage:
        trader = PaperTrader(starting_capital=1000.0, max_inventory=300.0)
        trader.load()          # load persisted state if any

        # Each cycle:
        fills = trader.process_cycle(quotes, snapshot, confidence)
        trader.save()
    """

    # Probability of a fill when our quote crosses the market price.
    # 15% models queue position — we're not always at the front.
    FILL_PROBABILITY = 0.15

    # Default quote size in shares
    BASE_QUOTE_SIZE = 10.0

    def __init__(
        self,
        starting_capital: float = 1000.0,
        max_inventory: float = 300.0,
        state_file: str = "data/paper_mm_state.json",
        fills_file: str = "data/paper_mm_fills.json",
    ):
        self.max_inventory = max_inventory
        self.state_file = state_file
        self.fills_file = fills_file
        self._all_fills: list[Fill] = []
        self.state = PaperState(
            starting_capital=starting_capital,
            cash=starting_capital,
            peak_capital=starting_capital,
        )

    # ── Persistence ───────────────────────────────────────────────────────────

    def load(self):
        """Load previous session state if it exists."""
        try:
            with open(self.state_file) as f:
                data = json.load(f)
            # Only restore capital/PnL fields, not current market data
            self.state.cash = data.get("cash", self.state.starting_capital)
            self.state.realized_pnl = data.get("realized_pnl", 0.0)
            self.state.net_inventory = data.get("net_inventory", 0.0)
            self.state.avg_entry_price = data.get("avg_entry_price", 0.0)
            self.state.total_fills = data.get("total_fills", 0)
            self.state.round_trips = data.get("round_trips", 0)
            self.state.winning_trips = data.get("winning_trips", 0)
            self.state.consecutive_losses = data.get("consecutive_losses", 0)
            self.state.peak_capital = data.get("peak_capital", self.state.starting_capital)
            self.state.max_drawdown = data.get("max_drawdown", 0.0)
            self.state.recent_fills = data.get("recent_fills", [])
            logger.info(f"Paper trader loaded: cash=${self.state.cash:.2f} pnl={self.state.realized_pnl:+.2f}")
        except FileNotFoundError:
            logger.info("Paper trader: no saved state found, starting fresh")
        except Exception as e:
            logger.warning(f"Paper trader load error: {e}, starting fresh")

    def reconcile_inventory(
        self,
        current_market_id: str | None,
        current_seconds_to_expiry: float,
    ) -> None:
        """
        Startup check: if the inventory loaded from disk belongs to a market
        that has already expired (different condition_id or zero time left),
        force-close it at the last known fair value so the session starts clean.

        Call this after load() once the Gamma feed has warmed up.
        """
        if abs(self.state.net_inventory) < 0.01:
            return  # nothing to reconcile

        saved_mid = self.state.market_id
        fv = self.state.current_fair_value or 0.5

        market_changed = bool(
            current_market_id and saved_mid and current_market_id != saved_mid
        )
        market_expired = current_seconds_to_expiry <= 0

        if market_changed or market_expired:
            reason = "market rolled over" if market_changed else "market already expired"
            saved_label   = saved_mid[:16]          if saved_mid          else "none"
            current_label = current_market_id[:16]  if current_market_id  else "none"
            logger.warning(
                f"Startup reconciliation: {reason} "
                f"(saved={saved_label!r}, current={current_label!r}). "
                f"Force-closing {self.state.net_inventory:+.0f}sh @ fair={fv:.4f}"
            )
            self._close_at_expiry(fv, saved_mid or "")
            self.save()

    def save(self):
        """Persist state to disk for dashboard and recovery."""
        try:
            Path(self.state_file).parent.mkdir(exist_ok=True)
            with open(self.state_file, "w") as f:
                json.dump(asdict(self.state), f, indent=2)
        except Exception as e:
            logger.warning(f"Paper trader save error: {e}")

    # ── Main cycle ────────────────────────────────────────────────────────────

    def process_cycle(
        self,
        quotes: dict,
        snapshot,
        confidence_result,
        market_id: str = "",
    ) -> list[Fill]:
        """
        Check for simulated fills against current market prices.

        quotes: output of EnhancedQuoteEngine.generate_quotes()
        snapshot: current SideDataSnapshot
        confidence_result: ConfidenceResult from ConfidenceCalculator
        """
        fills = []

        if confidence_result.tier == "PAUSED":
            self._update_display(quotes, snapshot, confidence_result, market_id)
            return fills

        our_bid = quotes.get("yes_bid", 0.0)
        our_ask = quotes.get("yes_ask", 1.0)
        fv = quotes.get("fair_value", 0.5)

        # Use real Gamma top-of-book if available, fall back to fair value estimate
        if snapshot.market_best_bid > 0 and snapshot.market_best_ask < 1.0:
            market_best_bid = snapshot.market_best_bid
            market_best_ask = snapshot.market_best_ask
        else:
            half_mkt = max(snapshot.market_spread / 2, 0.005)
            market_best_bid = max(0.01, fv - half_mkt)
            market_best_ask = min(0.99, fv + half_mkt)

        quote_size = self.BASE_QUOTE_SIZE * confidence_result.size_multiplier

        # Fill probability for a resting maker order:
        # A taker crosses our quote with probability that decays exponentially
        # as our quote moves further from the market best bid/ask.
        #
        # At our_bid == market_best_bid → full FILL_PROBABILITY (front of queue)
        # At our_bid 2¢ below best bid  → ~37% of FILL_PROBABILITY (behind queue)
        # At our_bid 6¢ below best bid  → ~5% of FILL_PROBABILITY (deep in book)
        #
        # This is the correct model: market makers post OUTSIDE the spread and
        # get filled when takers send orders deep enough to reach us.
        _DECAY = 0.02   # ¢-scale decay constant (2¢ = 1/e of base probability)

        bid_depth = max(0.0, market_best_bid - our_bid)
        bid_prob = self.FILL_PROBABILITY * math.exp(-bid_depth / _DECAY)
        bid_filled = False
        if our_bid > 0 and random.random() < bid_prob:
            fill = self._fill_bid(our_bid, quote_size, market_id)
            if fill:
                fills.append(fill)
                bid_filled = True

        ask_depth = max(0.0, our_ask - market_best_ask)
        ask_prob = self.FILL_PROBABILITY * math.exp(-ask_depth / _DECAY)
        if not bid_filled and our_ask < 1.0 and random.random() < ask_prob:
            fill = self._fill_ask(our_ask, quote_size, market_id)
            if fill:
                fills.append(fill)

        # Check for expiry: if market is expiring, close position
        if snapshot.seconds_to_expiry < 15 and abs(self.state.net_inventory) > 0:
            self._close_at_expiry(fv, market_id)

        self._update_unrealized(fv)
        self._update_display(quotes, snapshot, confidence_result, market_id)
        return fills

    # ── Fill execution ────────────────────────────────────────────────────────

    def _fill_bid(self, price: float, size: float, market_id: str) -> Optional[Fill]:
        """We bought YES shares at `price`."""
        cost = price * size
        if cost > self.state.cash:
            return None  # Can't afford

        if self.state.net_inventory + size > self.max_inventory:
            return None  # Would exceed inventory limit

        # Update inventory and cash
        pnl = 0.0
        if self.state.net_inventory >= 0:
            # Adding to long position — update average entry
            total_cost = self.state.avg_entry_price * self.state.net_inventory + cost
            self.state.net_inventory += size
            self.state.avg_entry_price = total_cost / self.state.net_inventory
        else:
            # Closing a short position — record P&L
            short_size = min(size, abs(self.state.net_inventory))
            if short_size > 0:
                pnl = (self.state.avg_entry_price - price) * short_size
                self._record_round_trip(pnl)
                self.state.realized_pnl += pnl
            self.state.net_inventory += size
            if self.state.net_inventory >= 0:
                self.state.avg_entry_price = price

        self.state.cash -= cost
        fee = net_fill_fee(price, size, is_maker=True)
        self.state.cash -= fee
        self.state.realized_pnl -= fee
        self.state.total_fills += 1

        fill = Fill(
            timestamp=time.time(),
            side="buy_yes",
            price=price,
            size=size,
            pnl=pnl,
            market_id=market_id,
        )
        self._record_fill(fill)
        logger.info(f"FILL BUY YES  {size:.0f}sh @ {price:.4f}  pnl={pnl:+.4f}  inv={self.state.net_inventory:+.0f}")
        return fill

    def _fill_ask(self, price: float, size: float, market_id: str) -> Optional[Fill]:
        """We sold YES shares at `price`."""
        if self.state.net_inventory - size < -self.max_inventory:
            return None  # Would exceed short inventory limit

        proceeds = price * size

        if self.state.net_inventory > 0:
            # Closing a long
            close_size = min(size, self.state.net_inventory)
            pnl = (price - self.state.avg_entry_price) * close_size
            self._record_round_trip(pnl)
            self.state.realized_pnl += pnl
        else:
            pnl = 0.0

        self.state.net_inventory -= size
        self.state.cash += proceeds
        fee = net_fill_fee(price, size, is_maker=True)
        self.state.cash -= fee
        self.state.realized_pnl -= fee
        self.state.total_fills += 1

        if self.state.net_inventory <= 0:
            self.state.avg_entry_price = price if self.state.net_inventory < 0 else 0.0

        fill = Fill(
            timestamp=time.time(),
            side="sell_yes",
            price=price,
            size=size,
            pnl=pnl,
            market_id=market_id,
        )
        self._record_fill(fill)
        logger.info(f"FILL SELL YES {size:.0f}sh @ {price:.4f}  pnl={pnl:+.4f}  inv={self.state.net_inventory:+.0f}")
        return fill

    def _close_at_expiry(self, resolution_price: float, market_id: str):
        """Force-close inventory at market expiry."""
        if abs(self.state.net_inventory) < 0.01:
            return
        inv = self.state.net_inventory
        pnl = (resolution_price - self.state.avg_entry_price) * inv
        self.state.realized_pnl += pnl
        self.state.cash += resolution_price * abs(inv)
        fee = net_fill_fee(resolution_price, abs(inv), is_maker=False)
        self.state.cash -= fee
        self.state.realized_pnl -= fee
        self._record_round_trip(pnl - fee)
        logger.info(f"EXPIRY CLOSE  {inv:+.0f}sh @ {resolution_price:.4f}  pnl={pnl:+.4f}  fee={fee:+.4f}")
        self.state.net_inventory = 0.0
        self.state.avg_entry_price = 0.0

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _record_round_trip(self, pnl: float):
        self.state.round_trips += 1
        if pnl > 0:
            self.state.winning_trips += 1
            self.state.consecutive_losses = 0
        else:
            self.state.consecutive_losses += 1

        # Update drawdown
        equity = self.state.cash + self.state.unrealized_pnl
        if equity > self.state.peak_capital:
            self.state.peak_capital = equity
        drawdown = self.state.peak_capital - equity
        if drawdown > self.state.max_drawdown:
            self.state.max_drawdown = drawdown

    def _update_unrealized(self, current_price: float):
        if self.state.net_inventory != 0:
            self.state.unrealized_pnl = (
                (current_price - self.state.avg_entry_price) * self.state.net_inventory
            )
        else:
            self.state.unrealized_pnl = 0.0

    def _record_fill(self, fill: Fill):
        fill_dict = asdict(fill)
        self.state.recent_fills.append(fill_dict)
        if len(self.state.recent_fills) > 100:
            self.state.recent_fills = self.state.recent_fills[-100:]
        try:
            Path(self.fills_file).parent.mkdir(exist_ok=True)
            with open(self.fills_file, "a") as f:
                f.write(json.dumps(fill_dict) + "\n")
        except Exception as e:
            logger.warning(f"Fill log write error: {e}")

    def _update_display(self, quotes, snapshot, confidence_result, market_id):
        self.state.current_yes_bid = quotes.get("yes_bid", 0.0)
        self.state.current_yes_ask = quotes.get("yes_ask", 1.0)
        self.state.current_fair_value = quotes.get("fair_value", 0.5)
        self.state.current_spread = quotes.get("spread", 0.0)
        self.state.current_confidence = confidence_result.score
        self.state.market_best_bid = snapshot.market_best_bid
        self.state.market_best_ask = snapshot.market_best_ask
        self.state.seconds_to_expiry = snapshot.seconds_to_expiry
        self.state.market_id = market_id
        self.state.last_update = time.time()

    # ── Read-only properties for dashboard ───────────────────────────────────

    @property
    def total_equity(self) -> float:
        return self.state.cash + self.state.unrealized_pnl

    @property
    def win_rate(self) -> float:
        if self.state.round_trips == 0:
            return 0.0
        return self.state.winning_trips / self.state.round_trips

    @property
    def total_pnl(self) -> float:
        return self.state.realized_pnl + self.state.unrealized_pnl
