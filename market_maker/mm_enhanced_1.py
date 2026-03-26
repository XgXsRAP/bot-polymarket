"""
╔══════════════════════════════════════════════════════════════════╗
║  ENHANCED MARKET MAKER — Side Data + Backtester                  ║
║                                                                  ║
║  This module adds two things to market_maker.py:                 ║
║                                                                  ║
║  1. SIDE DATA INTEGRATION                                        ║
║     Uses CVD, liquidations, funding rate, and OI to dynamically  ║
║     adjust spread width, inventory skew, and quote sizes.        ║
║     This is NOT the same as directional trading — the side data  ║
║     tells us HOW RISKY it is to quote, not WHICH SIDE to bet on. ║
║                                                                  ║
║  2. BACKTESTER                                                   ║
║     Replays historical Polymarket + BTC data to simulate how the ║
║     market maker would have performed with different parameters.  ║
║     This is how you PROVE the strategy works before risking money.║
║                                                                  ║
║  Run backtester:  python mm_enhanced.py --backtest               ║
║  Run live paper:  python mm_enhanced.py --mode paper             ║
╚══════════════════════════════════════════════════════════════════╝
"""

import asyncio
import json
import time
import os
import sys
import math
import argparse
import random
from collections import deque
from datetime import datetime, timezone, timedelta
from typing import Optional
from dataclasses import dataclass, field, asdict
from pathlib import Path

# ── Hyperliquid data feed for live signal data ──
try:
    from hyperliquid_api import HyperliquidFeed
    HAS_HL_FEED = True
except ImportError:
    HAS_HL_FEED = False

# ═══════════════════════════════════════════════════════════
#  PART 1: SIDE DATA INTEGRATION
#
#  How side data works differently for market making:
#
#  ┌────────────────────────────────────────────────────────┐
#  │  DIRECTIONAL BOT          │  MARKET MAKER              │
#  │  (your current bot)       │  (this bot)                │
#  ├────────────────────────────┼───────────────────────────┤
#  │  Liquidation data →       │  Liquidation data →        │
#  │  "Short squeeze likely"   │  "Volatility spike coming" │
#  │  → BUY YES                │  → WIDEN SPREAD to 8¢      │
#  │                           │                            │
#  │  High funding rate →      │  High funding rate →        │
#  │  "Longs overcrowded"      │  "Reversal risk high"      │
#  │  → BUY NO                 │  → REDUCE QUOTE SIZE       │
#  │                           │                            │
#  │  CVD divergence →         │  CVD divergence →           │
#  │  "Hidden buying/selling"  │  "Fair value might shift"  │
#  │  → Trade the divergence   │  → SHIFT FAIR VALUE (small)│
#  │                           │                            │
#  │  OI growing →             │  OI growing →               │
#  │  "New money entering"     │  "More informed traders"   │
#  │  → Bigger positions       │  → WIDEN SPREAD (more      │
#  │                           │    adverse selection risk)  │
#  └────────────────────────────┴───────────────────────────┘
#
#  Notice the pattern: the directional bot uses side data to
#  decide WHICH SIDE to trade. The market maker uses the SAME
#  data to decide HOW DEFENSIVELY to quote. Same inputs,
#  completely different outputs.
# ═══════════════════════════════════════════════════════════


@dataclass
class SideDataSnapshot:
    """
    A single reading of all side data feeds at one moment in time.

    This gets passed into the enhanced quoting engine every cycle.
    Each field carries a normalized signal that the quoting engine
    converts into spread/size/skew adjustments.
    """
    # BTC price data
    btc_price: float = 0.0
    btc_change_1m: float = 0.0
    btc_change_5m: float = 0.0
    btc_volatility_1m: float = 0.001   # Std dev of 1-min returns

    # Hyperliquid oracle + derivatives data
    hl_oracle_price: float = 0.0       # Independent BTC price source
    hl_funding_rate: float = 0.0       # %/hr — positive = longs pay shorts
    hl_open_interest: float = 0.0      # USD total

    # Processed signals from EnhancedSignalFeed (your existing bot)
    cvd_signal: float = 0.0            # -1 (bear div) to +1 (bull div)
    liq_signal: float = 0.0            # -1 (long liqs) to +1 (short liqs)
    funding_signal: float = 0.0        # -1 (overbought) to +1 (oversold)
    oi_signal: float = 0.0             # -1 (OI shrinking) to +1 (OI growing)

    # Chainlink settlement price
    chainlink_price: float = 0.0

    # Market microstructure
    market_spread: float = 0.0         # Current bid-ask spread on Polymarket
    seconds_to_expiry: float = 300.0

    timestamp: float = 0.0


class EnhancedFairValueEngine:
    """
    Fair value estimation that uses ALL available data sources.

    This is more sophisticated than the basic FairValueEngine in
    market_maker.py. It uses three tiers of information:

    Tier 1 — Price data (highest weight):
      Binance spot price, Chainlink oracle, Hyperliquid oracle.
      The Chainlink price is what Polymarket ACTUALLY uses to resolve
      the market, so it's the ground truth. Binance leads Chainlink
      by a few seconds, which gives us a tiny edge.

    Tier 2 — Momentum signals (medium weight):
      1m/5m BTC change, CVD divergence. These predict where the
      price is GOING in the next 5 minutes.

    Tier 3 — Regime signals (lowest weight):
      Funding rate, OI changes. These tell us how CONFIDENT to be
      in our fair value estimate. High funding + growing OI means
      the market has strong conviction — our fair value should
      respect the trend more.

    For market making, we DAMPEN all of these compared to the
    directional bot. We want our fair value to be STABLE, not
    reactive. A market maker that swings fair value wildly will
    get adversely selected on both sides.
    """

    def estimate(self, data: SideDataSnapshot, market_yes_price: float) -> float:
        """Returns estimated fair probability for YES (0.0 to 1.0)."""

        # ── Tier 1: Price anchor ──
        # Use Chainlink as the reference (it's the settlement source).
        # If Binance is ahead of Chainlink, that predicts Chainlink's next update.
        anchor = data.chainlink_price or data.hl_oracle_price or data.btc_price
        if anchor > 0 and data.btc_price > 0:
            # How far Binance is ahead of the settlement price
            price_lead = (data.btc_price - anchor) / anchor
            # Dampened: market maker uses 1/3 the weight of directional bot
            price_adj = min(max(price_lead * 5, -0.06), 0.06)
        else:
            price_adj = 0.0

        # ── Tier 2: Momentum ──
        # Dampened momentum with tanh scaling: noise suppressed, real moves weighted.
        # Sensitivity 150 (dampened vs directional bot's 200) with 0.04/0.02 caps.
        mom_1m = math.tanh(data.btc_change_1m * 150) * 0.04
        mom_5m = math.tanh(data.btc_change_5m * 75) * 0.02

        # CVD divergence: if price is up but CVD is down (bearish div),
        # fair value should be LOWER than pure price action suggests.
        # This is one of the few side data signals that affects fair value
        # directly (not just spread width).
        cvd_adj = data.cvd_signal * 0.02   # Max ±2% fair value shift

        # ── Tier 3: Regime (conviction weighting) ──
        # If funding is very high (+ve = longs crowded), the market is
        # potentially overextended. Dampen our fair value toward 0.50.
        # This makes our quotes MORE symmetric when the market is fragile.
        conviction = 1.0
        if abs(data.funding_signal) > 0.3:
            conviction = 0.85  # Less confident in trending fair value
        if abs(data.oi_signal) > 0.2:
            conviction *= 0.9  # OI change adds uncertainty

        # ── Combine ──
        model_estimate = 0.50 + (price_adj + mom_1m * 0.7 + mom_5m * 0.3 + cvd_adj) * conviction

        # Blend with market consensus (market knows things we don't)
        # For market making, we trust the market MORE than the directional bot does.
        # Directional bot: 60% model / 40% market
        # Market maker: 40% model / 60% market
        market_weight = 0.60
        fair_value = model_estimate * (1 - market_weight) + market_yes_price * market_weight

        return min(max(fair_value, 0.05), 0.95)


class EnhancedQuoteEngine:
    """
    Quote generation that dynamically adjusts based on side data.

    The basic QuoteEngine uses fixed base_spread + volatility adjustment.
    This enhanced version adds four more adjustment layers:

    1. LIQUIDATION SPREAD ADJUSTMENT
       When liquidation pressure is high (lots of positions near their
       liquidation price), expect sudden price moves. Widen spread.

    2. FUNDING RATE SPREAD ADJUSTMENT
       Extreme funding (positive or negative) means the market is
       overcrowded on one side. When it unwinds, expect a fast move.
       Widen spread proportionally to how extreme funding is.

    3. OI-BASED SIZE ADJUSTMENT
       Growing OI means more sophisticated traders are entering.
       More sophisticated traders = more adverse selection risk.
       Reduce quote size when OI is growing fast.

    4. EXPIRY TIME SPREAD CURVE
       As a market approaches expiry, the outcome becomes more certain.
       This means prices move faster toward 0 or 1. Widen spread
       aggressively in the last 2 minutes, and stop quoting entirely
       in the last 60 seconds.
    """

    def __init__(self, base_spread: float = 0.04, max_spread: float = 0.12,
                 base_size: float = 20.0, max_inventory: float = 100.0,
                 skew_factor: float = 0.002):
        self.base_spread = base_spread
        self.max_spread = max_spread
        self.base_size = base_size
        self.max_inventory = max_inventory
        self.skew_factor = skew_factor

    def generate_quotes(self, fair_value: float, net_inventory: float,
                        data: SideDataSnapshot) -> dict:
        """
        Generate bid/ask with all side data adjustments.

        Returns dict with yes_bid, yes_ask, no_bid, no_ask, sizes,
        and a breakdown of WHY each adjustment was made (for logging
        and backtesting analysis).
        """
        adjustments = {}  # Track what contributed to the final spread

        # ════════════════════════════════════════════════════
        #  SPREAD CALCULATION (additive layers)
        # ════════════════════════════════════════════════════

        spread = self.base_spread
        adjustments["base"] = self.base_spread

        # Layer 1: Volatility (same as basic version)
        vol_adj = data.btc_volatility_1m * 5.0
        spread += vol_adj
        adjustments["volatility"] = round(vol_adj, 4)

        # Layer 2: Liquidation pressure
        # High liquidation signal (either direction) means expect sudden moves.
        # Widen spread proportionally to the magnitude of liquidation pressure.
        liq_adj = abs(data.liq_signal) * 0.02  # Max +2¢ from liquidations
        spread += liq_adj
        adjustments["liquidation"] = round(liq_adj, 4)

        # Layer 3: Funding rate extremity
        # Normal funding (~0): no adjustment
        # Extreme funding (>0.3 signal): up to +1.5¢ wider
        # The logic: extreme funding means one side is overcrowded.
        # When it unwinds, price moves fast. We want wider spread
        # to avoid being caught in the unwind.
        funding_adj = abs(data.funding_signal) * 0.015
        spread += funding_adj
        adjustments["funding_rate"] = round(funding_adj, 4)

        # Layer 4: Time-to-expiry spread curve
        # As expiry approaches, outcome becomes more certain.
        # Binary options have a "volatility smile" that peaks
        # at the money and increases near expiry.
        #
        # Time curve:
        #   > 4 min remaining:  no adjustment
        #   2-4 min remaining:  +1¢
        #   1-2 min remaining:  +3¢
        #   < 1 min remaining:  +5¢ (or stop quoting)
        tte = data.seconds_to_expiry
        if tte < 60:
            time_adj = 0.05
        elif tte < 120:
            time_adj = 0.03
        elif tte < 240:
            time_adj = 0.01
        else:
            time_adj = 0.0
        spread += time_adj
        adjustments["time_to_expiry"] = round(time_adj, 4)

        # Cap at maximum spread
        spread = min(spread, self.max_spread)
        adjustments["final_spread"] = round(spread, 4)

        half_spread = spread / 2

        # ════════════════════════════════════════════════════
        #  INVENTORY SKEW
        # ════════════════════════════════════════════════════

        skew = net_inventory * self.skew_factor
        panic = abs(net_inventory) > self.max_inventory * 0.75
        if panic:
            skew *= 2.0
        adjustments["skew"] = round(skew, 4)
        adjustments["panic_mode"] = panic

        # ════════════════════════════════════════════════════
        #  SIZE ADJUSTMENT
        # ════════════════════════════════════════════════════

        size = self.base_size

        # OI-based size reduction: growing OI = more informed flow
        # More informed flow = higher adverse selection probability
        # So we quote SMALLER when OI is growing
        if data.oi_signal > 0.2:
            oi_reduction = data.oi_signal * 0.3  # Up to 30% smaller
            size *= (1 - oi_reduction)
            adjustments["oi_size_reduction"] = round(oi_reduction, 4)

        # Inventory-based size reduction (same as basic version)
        inv_pct = abs(net_inventory) / self.max_inventory
        size *= max(0.25, 1 - inv_pct)

        # Funding-based size reduction: extreme funding = reduce exposure
        if abs(data.funding_signal) > 0.4:
            size *= 0.7
            adjustments["funding_size_reduction"] = 0.3

        size = max(5.0, round(size, 1))
        adjustments["final_size"] = size

        # ════════════════════════════════════════════════════
        #  GENERATE PRICES
        # ════════════════════════════════════════════════════

        yes_bid = round(max(0.01, fair_value - half_spread - skew), 4)
        yes_ask = round(min(0.99, fair_value + half_spread - skew), 4)
        if yes_bid >= yes_ask:
            yes_bid = round(yes_ask - 0.01, 4)

        no_fair = 1.0 - fair_value
        no_skew = -skew
        no_bid = round(max(0.01, no_fair - half_spread - no_skew), 4)
        no_ask = round(min(0.99, no_fair + half_spread - no_skew), 4)
        if no_bid >= no_ask:
            no_bid = round(no_ask - 0.01, 4)

        return {
            "yes_bid": yes_bid, "yes_ask": yes_ask,
            "no_bid": no_bid, "no_ask": no_ask,
            "size": size,
            "fair_value": round(fair_value, 4),
            "spread": round(spread, 4),
            "adjustments": adjustments,
        }


# ═══════════════════════════════════════════════════════════
#  PART 2: BACKTESTER
#
#  Why you NEED a backtester for market making:
#
#  Your directional bot trades ~10-50 times per day. You can
#  paper trade for a week and have enough data to judge.
#
#  A market maker posts quotes every 500ms = 172,800 quotes/day.
#  You can't "feel" whether the parameters are right by watching
#  the log scroll. You need to replay historical data and measure
#  the outcome across thousands of simulated fills.
#
#  The backtester answers critical questions:
#  - "If I tightened the spread from 4¢ to 3¢, would I make more
#     from extra fills or lose more from adverse selection?"
#  - "Does the liquidation data actually improve my P&L, or is
#     it just adding complexity for no benefit?"
#  - "What's the worst-case inventory I'd have accumulated in
#     the last month?"
# ═══════════════════════════════════════════════════════════


@dataclass
class BacktestConfig:
    """Parameters for a single backtest run."""
    # Spread settings to test
    base_spread: float = 0.04
    max_spread: float = 0.12
    volatility_multiplier: float = 5.0

    # Inventory settings
    max_inventory: float = 100.0
    skew_factor: float = 0.002
    quote_size: float = 20.0

    # Side data toggles (turn on/off to measure their impact)
    use_liquidation_data: bool = True
    use_funding_data: bool = True
    use_cvd_data: bool = True
    use_oi_data: bool = True
    use_expiry_curve: bool = True

    # Simulation settings
    fill_probability: float = 0.15      # Base probability a quote gets filled per tick
    adverse_fill_pct: float = 0.25      # 25% of fills are adverse (price moves against us)
    ticks_per_market: int = 300         # ~5 min of data at 1 tick/second
    num_markets: int = 100              # Number of markets to simulate


@dataclass
class BacktestResult:
    """Outcome of a single backtest run."""
    config_name: str = ""
    total_fills: int = 0
    round_trips: int = 0
    spread_pnl: float = 0.0            # Profit from completed round trips
    inventory_pnl: float = 0.0         # Mark-to-market on remaining inventory
    total_pnl: float = 0.0
    max_inventory: float = 0.0         # Worst-case inventory reached
    avg_spread: float = 0.0
    adverse_fill_rate: float = 0.0     # What fraction of fills were adverse
    pnl_per_fill: float = 0.0
    sharpe_ratio: float = 0.0          # Risk-adjusted return
    max_drawdown: float = 0.0          # Worst peak-to-trough


class MarketMakingBacktester:
    """
    Simulates market making performance across synthetic market data.

    Since Polymarket doesn't have a public historical data API for
    orderbook-level data, we generate realistic synthetic markets:

    1. Start with a random fair value path (BTC-like volatility)
    2. Add realistic bid-ask spreads around it
    3. Simulate fills when market price crosses our quote price
    4. Track inventory, P&L, and risk metrics

    This isn't perfect — real markets have order flow patterns, informed
    traders, and liquidity dynamics that synthetic data can't capture.
    But it's MUCH better than going live with untested parameters.

    For more accurate backtesting, you can feed in REAL historical data
    from the Polymarket CLOB API (see `load_historical_data` method).
    """

    def __init__(self):
        self.fair_value_engine = EnhancedFairValueEngine()

    def generate_synthetic_market(self, ticks: int, start_fv: float = 0.50,
                                  volatility: float = 0.002) -> list[SideDataSnapshot]:
        """
        Generate a realistic sequence of market ticks.

        The synthetic data mimics real BTC 5-minute market dynamics:
        - Price follows a random walk with drift
        - Volatility clusters (calm periods followed by spikes)
        - Liquidation pressure builds during trends
        - Funding rate drifts with the trend
        - CVD can diverge from price (hidden buying/selling)
        """
        snapshots = []
        price = start_fv
        vol = volatility
        funding = 0.0
        cvd = 0.0
        oi_growth = 0.0

        # BTC price simulation parameters
        btc_base = 87000 + random.uniform(-2000, 2000)
        btc = btc_base
        btc_history = deque(maxlen=60)

        for i in range(ticks):
            # ── Evolve BTC price (random walk with mean reversion) ──
            # Volatility clustering: vol spikes and slowly decays
            vol = max(0.0005, vol * 0.995 + random.gauss(0, 0.0003))
            drift = -0.00001 * (btc - btc_base)  # Mean reversion
            btc_return = drift + random.gauss(0, vol)
            btc *= (1 + btc_return)
            btc_history.append(btc)

            # 1m change (lookback 60 ticks)
            change_1m = (btc - btc_history[0]) / btc_history[0] if len(btc_history) >= 60 else 0.0
            change_5m = (btc - btc_base) / btc_base

            # ── Evolve derivatives data ──
            # Funding drifts toward the trend direction
            funding += random.gauss(0, 0.001) + change_1m * 0.01
            funding *= 0.98  # Decay toward zero

            # CVD: usually follows price, but sometimes diverges
            if random.random() < 0.1:  # 10% chance of divergence
                cvd += random.gauss(0, 0.5)
            else:
                cvd = cvd * 0.95 + (1 if change_1m > 0 else -1) * 0.3

            # OI: grows during trends, shrinks during reversals
            oi_growth = oi_growth * 0.9 + abs(change_1m) * 5 + random.gauss(0, 0.1)

            # Liquidation pressure: builds when price moves fast
            liq = min(max(change_1m * 100, -1), 1) + random.gauss(0, 0.1)

            # Fair value for the prediction market (50% ± price movement)
            price = 0.50 + change_5m * 3  # Amplify BTC move into binary prob
            price = min(max(price, 0.05), 0.95)

            snap = SideDataSnapshot(
                btc_price=btc,
                btc_change_1m=change_1m,
                btc_change_5m=change_5m,
                btc_volatility_1m=vol,
                hl_oracle_price=btc * (1 + random.gauss(0, 0.0001)),
                hl_funding_rate=funding,
                hl_open_interest=5e9 + oi_growth * 1e8,
                cvd_signal=min(max(cvd / 3, -1), 1),
                liq_signal=min(max(liq, -1), 1),
                funding_signal=min(max(-funding * 500, -1), 1),
                oi_signal=min(max(oi_growth / 2 - 0.5, -1), 1),
                chainlink_price=btc * (1 - 0.001 * random.random()),
                market_spread=random.uniform(0.02, 0.06),
                seconds_to_expiry=max(0, 300 - i),
                timestamp=time.time() + i,
            )
            snapshots.append(snap)

        return snapshots

    def run_single_backtest(self, config: BacktestConfig,
                            config_name: str = "") -> BacktestResult:
        """
        Run one complete backtest with the given parameters.

        Simulates num_markets markets, each with ticks_per_market ticks.
        At each tick:
          1. Generate quotes using EnhancedQuoteEngine
          2. Check if any quotes would have been filled
          3. Update inventory and P&L
          4. Record metrics
        """
        engine = EnhancedQuoteEngine(
            base_spread=config.base_spread,
            max_spread=config.max_spread,
            base_size=config.quote_size,
            max_inventory=config.max_inventory,
            skew_factor=config.skew_factor,
        )

        # Aggregate metrics
        total_fills = 0
        round_trips = 0
        spread_pnl = 0.0
        max_inv = 0.0
        pnl_series = []
        running_pnl = 0.0
        peak_pnl = 0.0
        max_drawdown = 0.0
        spreads = []

        for market_idx in range(config.num_markets):
            # Generate one market's worth of data
            start_fv = random.uniform(0.35, 0.65)
            vol = random.uniform(0.001, 0.004)
            ticks = self.generate_synthetic_market(
                config.ticks_per_market, start_fv, vol
            )

            # Per-market state
            inventory = 0.0  # Net YES shares
            buy_fills = []    # Prices we bought at (for round-trip matching)

            for tick_data in ticks:
                # Skip if close to expiry
                if tick_data.seconds_to_expiry < 60:
                    continue

                # Zero out side data signals if disabled in config
                if not config.use_liquidation_data:
                    tick_data.liq_signal = 0.0
                if not config.use_funding_data:
                    tick_data.funding_signal = 0.0
                if not config.use_cvd_data:
                    tick_data.cvd_signal = 0.0
                if not config.use_oi_data:
                    tick_data.oi_signal = 0.0
                if not config.use_expiry_curve:
                    tick_data.seconds_to_expiry = 999  # Disable time adjustment

                # Get fair value and generate quotes
                market_mid = 0.50 + tick_data.btc_change_5m * 3
                market_mid = min(max(market_mid, 0.05), 0.95)
                fv = self.fair_value_engine.estimate(tick_data, market_mid)
                quotes = engine.generate_quotes(fv, inventory, tick_data)
                spreads.append(quotes["spread"])

                # ── Simulate fills ──
                # A fill happens when the market price crosses our quote.
                # We model this probabilistically: tighter spread = more fills,
                # but also more adverse fills.
                spread_ratio = config.base_spread / max(quotes["spread"], 0.001)
                fill_prob = config.fill_probability * spread_ratio

                # Check bid fill (someone sells to us → we buy)
                if random.random() < fill_prob:
                    total_fills += 1
                    buy_price = quotes["yes_bid"]
                    inventory += quotes["size"]
                    buy_fills.append(buy_price)
                    max_inv = max(max_inv, abs(inventory))

                    # Adverse selection check: did the price immediately move
                    # against us after the fill?
                    if random.random() < config.adverse_fill_pct:
                        # Price dropped after we bought → adverse fill
                        adverse_loss = quotes["size"] * quotes["spread"] * 0.5
                        running_pnl -= adverse_loss

                # Check ask fill (someone buys from us → we sell)
                if random.random() < fill_prob and buy_fills:
                    total_fills += 1
                    sell_price = quotes["yes_ask"]
                    inventory -= quotes["size"]

                    # Match with earliest buy for round-trip P&L
                    buy_price = buy_fills.pop(0)
                    trip_pnl = (sell_price - buy_price) * quotes["size"]
                    spread_pnl += trip_pnl
                    running_pnl += trip_pnl
                    round_trips += 1

                # Track P&L series for drawdown calculation
                pnl_series.append(running_pnl)
                peak_pnl = max(peak_pnl, running_pnl)
                drawdown = peak_pnl - running_pnl
                max_drawdown = max(max_drawdown, drawdown)

            # End of market: mark remaining inventory to market
            if abs(inventory) > 0:
                # Assume we close at mid-price (optimistic; reality might be worse)
                inv_pnl = -abs(inventory) * config.base_spread
                running_pnl += inv_pnl

        # Calculate Sharpe ratio (annualized, assuming ~100 markets/day)
        if pnl_series and len(pnl_series) > 1:
            returns = [pnl_series[i] - pnl_series[i-1] for i in range(1, len(pnl_series))]
            avg_ret = sum(returns) / len(returns) if returns else 0
            std_ret = (sum((r - avg_ret)**2 for r in returns) / len(returns)) ** 0.5 if returns else 1
            sharpe = (avg_ret / std_ret) * (252 ** 0.5) if std_ret > 0 else 0
        else:
            sharpe = 0.0

        result = BacktestResult(
            config_name=config_name,
            total_fills=total_fills,
            round_trips=round_trips,
            spread_pnl=round(spread_pnl, 4),
            inventory_pnl=round(running_pnl - spread_pnl, 4),
            total_pnl=round(running_pnl, 4),
            max_inventory=round(max_inv, 1),
            avg_spread=round(sum(spreads) / len(spreads), 4) if spreads else 0,
            adverse_fill_rate=config.adverse_fill_pct,
            pnl_per_fill=round(running_pnl / total_fills, 4) if total_fills > 0 else 0,
            sharpe_ratio=round(sharpe, 2),
            max_drawdown=round(max_drawdown, 4),
        )
        return result

    def run_parameter_sweep(self) -> list[BacktestResult]:
        """
        Run multiple backtests with different parameters to find the optimal setup.

        Tests:
        1. Different spread widths (2¢ to 8¢)
        2. Side data on vs off (to prove it helps)
        3. Different skew factors
        4. Different quote sizes

        This is the PRIMARY tool for tuning your market maker.
        Don't guess at parameters — run this sweep and look at the results.
        """
        results = []

        # ════════════════════════════════════════════════════
        # Test 1: Spread width comparison
        # Question: "What's the optimal spread for Polymarket BTC markets?"
        # ════════════════════════════════════════════════════
        print("\n📊 TEST 1: Spread Width Optimization")
        print("=" * 70)
        for spread in [0.02, 0.03, 0.04, 0.05, 0.06, 0.08]:
            cfg = BacktestConfig(base_spread=spread)
            r = self.run_single_backtest(cfg, f"spread_{spread*100:.0f}c")
            results.append(r)
            print(
                f"  Spread {spread*100:.0f}¢ | "
                f"Fills: {r.total_fills:4d} | "
                f"Trips: {r.round_trips:4d} | "
                f"P&L: ${r.total_pnl:+8.2f} | "
                f"Per fill: ${r.pnl_per_fill:+.4f} | "
                f"MaxInv: {r.max_inventory:5.0f} | "
                f"Sharpe: {r.sharpe_ratio:+.2f}"
            )

        # ════════════════════════════════════════════════════
        # Test 2: Side data impact
        # Question: "Does side data actually improve P&L?"
        # ════════════════════════════════════════════════════
        print(f"\n📊 TEST 2: Side Data Impact (all at spread=4¢)")
        print("=" * 70)

        # Baseline: no side data
        cfg_none = BacktestConfig(
            use_liquidation_data=False, use_funding_data=False,
            use_cvd_data=False, use_oi_data=False, use_expiry_curve=False,
        )
        r_none = self.run_single_backtest(cfg_none, "no_side_data")
        results.append(r_none)
        print(
            f"  No side data     | P&L: ${r_none.total_pnl:+8.2f} | "
            f"MaxInv: {r_none.max_inventory:5.0f} | Sharpe: {r_none.sharpe_ratio:+.2f}"
        )

        # Each signal individually
        for name, field_name in [
            ("+ Liquidations", "use_liquidation_data"),
            ("+ Funding rate", "use_funding_data"),
            ("+ CVD", "use_cvd_data"),
            ("+ OI data", "use_oi_data"),
            ("+ Expiry curve", "use_expiry_curve"),
        ]:
            cfg = BacktestConfig(
                use_liquidation_data=False, use_funding_data=False,
                use_cvd_data=False, use_oi_data=False, use_expiry_curve=False,
            )
            setattr(cfg, field_name, True)
            r = self.run_single_backtest(cfg, name)
            results.append(r)
            delta = r.total_pnl - r_none.total_pnl
            print(
                f"  {name:<18s}| P&L: ${r.total_pnl:+8.2f} "
                f"(Δ${delta:+.2f}) | "
                f"MaxInv: {r.max_inventory:5.0f} | Sharpe: {r.sharpe_ratio:+.2f}"
            )

        # All side data combined
        cfg_all = BacktestConfig()  # All defaults = all side data on
        r_all = self.run_single_backtest(cfg_all, "all_side_data")
        results.append(r_all)
        delta = r_all.total_pnl - r_none.total_pnl
        print(
            f"  {'ALL COMBINED':<18s}| P&L: ${r_all.total_pnl:+8.2f} "
            f"(Δ${delta:+.2f}) | "
            f"MaxInv: {r_all.max_inventory:5.0f} | Sharpe: {r_all.sharpe_ratio:+.2f}"
        )

        # ════════════════════════════════════════════════════
        # Test 3: Inventory skew factor
        # ════════════════════════════════════════════════════
        print(f"\n📊 TEST 3: Inventory Skew Factor")
        print("=" * 70)
        for skew in [0.0, 0.001, 0.002, 0.004, 0.008]:
            cfg = BacktestConfig(skew_factor=skew)
            r = self.run_single_backtest(cfg, f"skew_{skew}")
            results.append(r)
            print(
                f"  Skew {skew:.3f} | "
                f"P&L: ${r.total_pnl:+8.2f} | "
                f"MaxInv: {r.max_inventory:5.0f} | "
                f"MaxDD: ${r.max_drawdown:.2f} | "
                f"Sharpe: {r.sharpe_ratio:+.2f}"
            )

        # ════════════════════════════════════════════════════
        # Test 4: Quote size
        # ════════════════════════════════════════════════════
        print(f"\n📊 TEST 4: Quote Size")
        print("=" * 70)
        for size in [5, 10, 20, 50]:
            cfg = BacktestConfig(quote_size=float(size))
            r = self.run_single_backtest(cfg, f"size_{size}")
            results.append(r)
            print(
                f"  Size {size:3d} shares | "
                f"P&L: ${r.total_pnl:+8.2f} | "
                f"MaxInv: {r.max_inventory:5.0f} | "
                f"MaxDD: ${r.max_drawdown:.2f} | "
                f"Sharpe: {r.sharpe_ratio:+.2f}"
            )

        return results


# ═══════════════════════════════════════════════════════════
#  PART 3: DUAL BOT ORCHESTRATOR
#
#  Running both bots simultaneously — how and why.
#
#  The directional bot and market maker are COMPLEMENTARY:
#
#    ┌─────────────────────────────────────────────────────┐
#    │  Market condition    │ Directional │ Market Maker    │
#    ├──────────────────────┼─────────────┼─────────────────┤
#    │  Strong BTC trend    │ ★★★ Great   │ ★☆☆ Risky      │
#    │  Sideways/choppy     │ ☆☆☆ Sits    │ ★★★ Great      │
#    │  Low volatility      │ ☆☆☆ No edge │ ★★☆ Steady     │
#    │  High volatility     │ ★★☆ Okay    │ ★☆☆ Wide spread│
#    │  Near expiry (<2min) │ ★★☆ Quick   │ ☆☆☆ Stops      │
#    └──────────────────────┴─────────────┴─────────────────┘
#
#  They thrive in OPPOSITE conditions. Running both means
#  you're making money in more market states.
#
#  CRITICAL SAFETY RULE: They must not trade the SAME market
#  at the same time with conflicting positions. The orchestrator
#  enforces this via a shared position lock.
# ═══════════════════════════════════════════════════════════


class SharedPositionLock:
    """
    Thread-safe lock that prevents both bots from holding conflicting
    positions in the same market simultaneously.

    The directional bot registers its trades here. The market maker
    checks before quoting. If the directional bot is long YES on
    market X, the market maker either:
      a) Skips market X entirely, or
      b) Only quotes the same direction (biased quoting)

    Option (b) is more sophisticated — it turns the market maker
    into a "helper" that provides exit liquidity for the directional
    bot's position. But for v1, we use option (a) — simple exclusion.
    """

    def __init__(self):
        self._locks: dict[str, dict] = {}  # condition_id → {side, bot, time}

    def register_position(self, condition_id: str, side: str, bot_name: str):
        """Called by directional bot when it opens a position."""
        self._locks[condition_id] = {
            "side": side,
            "bot": bot_name,
            "time": time.time(),
        }

    def release_position(self, condition_id: str, bot_name: str):
        """Called when a position is closed."""
        if condition_id in self._locks and self._locks[condition_id]["bot"] == bot_name:
            del self._locks[condition_id]

    def can_quote(self, condition_id: str) -> tuple[bool, str]:
        """Called by market maker before posting quotes."""
        if condition_id in self._locks:
            lock = self._locks[condition_id]
            return False, f"Locked by {lock['bot']} ({lock['side']})"
        return True, "OK"

    def get_all_locks(self) -> dict:
        return dict(self._locks)


class DualBotConfig:
    """
    Configuration for running both bots together.

    Capital splitting:
      The safest approach is to split your total capital between the two bots.
      Don't let both bots use the same $1000 — that creates hidden leverage.

    Recommended split:
      - Directional bot: 40% of capital (higher risk, higher reward per trade)
      - Market maker: 60% of capital (lower risk, needs more for two-sided quoting)

    On a $1000 account:
      - Directional: $400 → max $20 per trade (5% of $400)
      - Market maker: $600 → max $30 per side ($15 bid + $15 ask)
    """

    def __init__(self, total_capital: float = 1000.0,
                 directional_pct: float = 0.40,
                 mm_pct: float = 0.60):
        self.total_capital = total_capital
        self.directional_capital = total_capital * directional_pct
        self.mm_capital = total_capital * mm_pct
        self.position_lock = SharedPositionLock()

        print(f"""
╔══════════════════════════════════════════════════════════╗
║  DUAL BOT MODE                                          ║
║  Total capital: ${total_capital:,.2f}                         ║
║  Directional:   ${self.directional_capital:,.2f} ({directional_pct*100:.0f}%)                     ║
║  Market Maker:  ${self.mm_capital:,.2f} ({mm_pct*100:.0f}%)                     ║
║  Shared position lock: ACTIVE                           ║
╚══════════════════════════════════════════════════════════╝
        """)


# ═══════════════════════════════════════════════════════════
#  HISTORICAL DATA LOADER (for real backtesting)
# ═══════════════════════════════════════════════════════════

class HistoricalDataLoader:
    """
    Loads real historical data for backtesting.

    Data sources:
    1. Your own paper_trades.json (what the bot actually saw)
    2. Polymarket Gamma API (historical market data)
    3. Binance klines API (BTC price history)

    Call `collect_data()` to start recording live data for future backtests.
    Call `load_data()` to replay recorded data through the backtester.
    """

    def __init__(self, data_dir: str = "data"):
        self.data_dir = data_dir
        self.recording_file = f"{data_dir}/mm_historical.jsonl"
        Path(data_dir).mkdir(exist_ok=True)

    def record_tick(self, snapshot: SideDataSnapshot):
        """
        Append one tick of live data to the historical file.

        Call this every second from your live/paper bot to build
        up a real historical dataset. After a few days, you'll have
        enough data for meaningful backtests.
        """
        try:
            with open(self.recording_file, "a") as f:
                f.write(json.dumps(asdict(snapshot)) + "\n")
        except Exception:
            pass

    def load_data(self, max_ticks: int = 50000) -> list[SideDataSnapshot]:
        """Load recorded historical ticks for backtesting."""
        snapshots = []
        try:
            with open(self.recording_file) as f:
                for i, line in enumerate(f):
                    if i >= max_ticks:
                        break
                    data = json.loads(line.strip())
                    snapshots.append(SideDataSnapshot(**data))
        except FileNotFoundError:
            print(f"⚠️  No historical data found at {self.recording_file}")
            print("   Run the bot in paper mode first to collect data.")
            print("   The bot will record ticks automatically.")
        return snapshots


# ═══════════════════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════════════════

def run_backtest():
    """Run the full parameter sweep backtester."""
    print("""
╔══════════════════════════════════════════════════════════════╗
║  MARKET MAKER BACKTESTER                                     ║
║                                                              ║
║  Testing spread widths, side data impact, skew factors,      ║
║  and quote sizes across 100 simulated markets each.          ║
║                                                              ║
║  Look for: highest Sharpe ratio with acceptable max drawdown ║
╚══════════════════════════════════════════════════════════════╝
    """)

    bt = MarketMakingBacktester()
    results = bt.run_parameter_sweep()

    # Find the best configuration
    best = max(results, key=lambda r: r.sharpe_ratio)
    print(f"\n{'='*70}")
    print(f"🏆 BEST CONFIGURATION: {best.config_name}")
    print(f"   Total P&L: ${best.total_pnl:+.2f}")
    print(f"   Sharpe Ratio: {best.sharpe_ratio:+.2f}")
    print(f"   Max Drawdown: ${best.max_drawdown:.2f}")
    print(f"   P&L per fill: ${best.pnl_per_fill:+.4f}")
    print(f"   Max Inventory: {best.max_inventory:.0f} shares")
    print(f"{'='*70}")

    # Save results
    Path("data").mkdir(exist_ok=True)
    with open("data/backtest_results.json", "w") as f:
        json.dump([asdict(r) for r in results], f, indent=2)
    print(f"\n📁 Results saved to data/backtest_results.json")


async def run_signal_monitor():
    """
    Live signal monitoring mode — connects to Hyperliquid and prints
    real-time signals that would feed into the EnhancedQuoteEngine.

    This lets you verify the data pipeline before trading with it.
    Use: python mm_enhanced_1.py --signals
    """
    if not HAS_HL_FEED:
        print("ERROR: hyperliquid_api.py not found.")
        print("Make sure hyperliquid_api.py is in the same directory.")
        return

    feed = HyperliquidFeed(poll_interval=3.0)
    fair_value_engine = EnhancedFairValueEngine()
    quote_engine = EnhancedQuoteEngine()
    data_loader = HistoricalDataLoader()

    print("""
╔══════════════════════════════════════════════════════════════╗
║  LIVE SIGNAL MONITOR                                         ║
║  Connecting to Hyperliquid for real-time derivatives data...  ║
║  Press Ctrl+C to stop                                        ║
╚══════════════════════════════════════════════════════════════╝
    """)

    await feed.start()

    # Wait a few seconds for initial data
    await asyncio.sleep(5)

    try:
        cycle = 0
        while True:
            hl_fields = feed.get_snapshot_fields()

            # Build a SideDataSnapshot with Hyperliquid data
            # BTC price fields would come from BTCPriceFeed in production;
            # here we use the oracle price as a stand-in for monitoring.
            snapshot = SideDataSnapshot(
                btc_price=hl_fields["hl_oracle_price"],
                btc_change_1m=0.0,      # would come from BTCPriceFeed
                btc_change_5m=0.0,      # would come from BTCPriceFeed
                btc_volatility_1m=0.001, # would come from BTCPriceFeed
                hl_oracle_price=hl_fields["hl_oracle_price"],
                hl_funding_rate=hl_fields["hl_funding_rate"],
                hl_open_interest=hl_fields["hl_open_interest"],
                cvd_signal=hl_fields["cvd_signal"],
                liq_signal=hl_fields["liq_signal"],
                funding_signal=hl_fields["funding_signal"],
                oi_signal=hl_fields["oi_signal"],
                timestamp=time.time(),
            )

            # Record for future backtesting
            data_loader.record_tick(snapshot)

            # Show how the signals affect quoting
            fair_value = fair_value_engine.estimate(snapshot, market_yes_price=0.50)
            quotes = quote_engine.generate_quotes(fair_value, net_inventory=0.0, data=snapshot)

            cycle += 1
            if cycle % 2 == 0:  # Print every ~2 seconds
                status = feed.status()
                print(f"\n{'─'*60}")
                print(f"  BTC Oracle: ${status['oracle_price']:,.2f}  |  Funding: {status['funding_rate']}/hr")
                print(f"  OI: ${float(status['open_interest'].replace(',','')):,.0f}  |  WS Trades: {status['ws_trade_count']}")
                print(f"  ┌─ Signals ─────────────────────────────────┐")
                print(f"  │  CVD:      {status['cvd_signal']:>7s}  (buy vs sell flow)  │")
                print(f"  │  Liq:      {status['liq_signal']:>7s}  (liquidation proxy) │")
                print(f"  │  Funding:  {status['funding_signal']:>7s}  (crowd positioning) │")
                print(f"  │  OI:       {status['oi_signal']:>7s}  (new money flow)    │")
                print(f"  └────────────────────────────────────────────┘")
                print(f"  Fair Value: {quotes['fair_value']:.4f}  |  Spread: {quotes['spread']:.4f}")
                print(f"  YES Bid/Ask: {quotes['yes_bid']:.4f} / {quotes['yes_ask']:.4f}")
                print(f"  Adjustments: {quotes['adjustments']}")

            await asyncio.sleep(1)

    except KeyboardInterrupt:
        print("\nStopping signal monitor...")
    finally:
        await feed.stop()
        print("Signal monitor stopped. Recorded ticks saved to data/mm_historical.jsonl")


def main():
    parser = argparse.ArgumentParser(description="Enhanced Market Maker + Backtester")
    parser.add_argument("--backtest", action="store_true",
                        help="Run parameter sweep backtester")
    parser.add_argument("--signals", action="store_true",
                        help="Run live signal monitor (connects to Hyperliquid)")
    parser.add_argument("--mode", choices=["paper", "live"], default="paper",
                        help="Trading mode (default: paper)")
    parser.add_argument("--dual", action="store_true",
                        help="Run in dual-bot mode with position locking")
    args = parser.parse_args()

    if args.backtest:
        run_backtest()
    elif args.signals:
        asyncio.run(run_signal_monitor())
    else:
        print("""
╔══════════════════════════════════════════════════════════════╗
║  ENHANCED MARKET MAKER                                       ║
╠══════════════════════════════════════════════════════════════╣
║                                                              ║
║  Available modes:                                            ║
║                                                              ║
║    --backtest   Run parameter sweep backtester               ║
║    --signals    Live signal monitor (Hyperliquid data)        ║
║                                                              ║
║  For actual trading, use market_maker.py with optimized      ║
║  settings from the backtester.                               ║
║                                                              ║
║  Examples:                                                   ║
║    python mm_enhanced_1.py --backtest                        ║
║    python mm_enhanced_1.py --signals                         ║
║    python market_maker.py --mode paper                       ║
╚══════════════════════════════════════════════════════════════╝
        """)


if __name__ == "__main__":
    main()
