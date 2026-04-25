"""
╔══════════════════════════════════════════════════════════════════╗
║  POLYMARKET MARKET MAKER                                         ║
║                                                                  ║
║  Autonomous market-making bot for BTC 5-minute UP/DOWN markets.  ║
║  Quotes two-sided limit orders using real-time side data from    ║
║  Hyperliquid (CVD, funding, OI, liquidations) and Chainlink      ║
║  on-chain BTC price as the settlement reference.                 ║
║                                                                  ║
║  Side data adjusts spread width, inventory skew, and quote size  ║
║  dynamically — NOT directional signals. The bot earns maker      ║
║  rebates rather than paying taker fees.                          ║
║                                                                  ║
║  Modes:                                                          ║
║    python mm_enhanced_1.py --paper      Paper trading (no funds)  ║
║    python mm_enhanced_1.py --live       Live CLOB orders          ║
║    python mm_enhanced_1.py --dual       Live + position lock      ║
║    python mm_enhanced_1.py --signals    Signal monitor only       ║
║    python mm_enhanced_1.py --backtest   Parameter sweep           ║
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
from loguru import logger

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv optional; env vars can still be set externally

# ── Hyperliquid data feed for live signal data ──
try:
    from hyperliquid_api import HyperliquidFeed
    HAS_HL_FEED = True
except ImportError:
    HAS_HL_FEED = False

# ── Binance BTC spot price feed ──
try:
    from binance_feed import BinanceBTCFeed
    HAS_BINANCE_FEED = True
except ImportError:
    HAS_BINANCE_FEED = False

# ── Polymarket Gamma API feed ──
try:
    from polymarket_gamma import PolymarketGammaFeed
    HAS_GAMMA_FEED = True
except ImportError:
    HAS_GAMMA_FEED = False

# ── Chainlink BTC/USD settlement feed ──
try:
    from chainlink_feed import ChainlinkBTCFeed
    HAS_CHAINLINK_FEED = True
except ImportError:
    HAS_CHAINLINK_FEED = False

# ── Confidence scoring + paper trading ──
from confidence import ConfidenceCalculator
from paper_trader import PaperTrader

# ── Live CLOB order manager (optional — requires py-clob-client) ──
try:
    from live_order_manager import LiveOrderManager
    HAS_LIVE_TRADER = True
except ImportError:
    HAS_LIVE_TRADER = False

# ── Alerting (Telegram / Discord) ──
from alerting import AlertManager

# ── Polymarket dynamic fee model ──
from fees import (
    polymarket_taker_fee, polymarket_taker_fee_amount,
    polymarket_maker_rebate_amount, net_fill_fee, minimum_profitable_spread,
    GAS_COST_PER_TX,
)

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
    market_best_bid: float = 0.0       # Gamma top-of-book bid
    market_best_ask: float = 1.0       # Gamma top-of-book ask
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
        # CVD divergence: if price is up but CVD is down (bearish divergence),
        # fair value should lean lower. Small coefficient (0.01) keeps FV stable —
        # CVD is a directional hint, not a conviction signal.
        # Previously disabled based on flawed synthetic backtest (ask-fills-after-bid-only).
        # Re-enabled with dampened weight for live evaluation.
        cvd_adj = data.cvd_signal * 0.01

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

        # Blend with market consensus (market knows things we don't).
        # Base: 60% market / 40% model (market maker default — stable quotes).
        # When funding + momentum signals are strongly aligned, trust the model
        # more — reduce market weight by up to 20 percentage points.
        signal_strength = (abs(data.funding_signal) + abs(data.btc_change_5m / 0.01)) / 2
        signal_strength = min(1.0, signal_strength)  # clamp to [0, 1]
        market_weight = 0.60 - 0.20 * signal_strength  # ranges 0.40–0.60
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

    def __init__(self, base_spread: float = 0.06, max_spread: float = 0.12,
                 base_size: float = 5.0, max_inventory: float = 100.0,
                 skew_factor: float = 0.0):
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

        # Layer 1: Volatility — adaptive spread based on realized 1-min vol.
        # A calm market (vol ~0.001) adds ~0.5¢. A spike (vol ~0.005) adds ~2.5¢.
        # When volatility is above 3× the base rate (0.001), we treat it as a
        # spike and double the vol_adj to protect against adverse selection.
        vol_base = 0.001
        if data.btc_volatility_1m > vol_base * 3:
            vol_adj = data.btc_volatility_1m * 10.0   # spike: widen aggressively
        else:
            vol_adj = data.btc_volatility_1m * 5.0    # normal: standard widening
        spread += vol_adj
        adjustments["volatility"] = round(vol_adj, 4)

        # Layer 2: Liquidation pressure
        # High liquidation signal (either direction) means expect sudden moves.
        # Widen spread proportionally to the magnitude of liquidation pressure.
        liq_adj = abs(data.liq_signal) * 0.02  # Max +2¢ from liquidations
        spread += liq_adj
        adjustments["liquidation"] = round(liq_adj, 4)

        # Layer 2.5: CVD-based spread widening (NOT fair value — spread only)
        # High absolute CVD means one-sided flow — higher adverse selection risk.
        cvd_spread_adj = abs(data.cvd_signal) * 0.01  # Max +1c from one-sided flow
        spread += cvd_spread_adj
        adjustments["cvd_spread"] = round(cvd_spread_adj, 4)

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
        #
        # Extra: if vol is spiking AND we're inside the last 90 seconds,
        # max out the spread — we don't want to be caught in a resolution
        # lottery when BTC is moving hard.
        tte = data.seconds_to_expiry
        if tte < 60:
            time_adj = 0.05
        elif tte < 90 and data.btc_volatility_1m > vol_base * 3:
            # High vol + imminent expiry: treat same as <60s (max spread)
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

        # Fee floor: never quote tighter than cost to fill
        fee_floor = minimum_profitable_spread(fair_value) + 0.001
        spread = max(spread, fee_floor)
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

        # Anchor quotes to the MARKET mid, not internal fair_value.
        # Internal fair_value is used only as a directional lean (capped at ±2¢).
        # This ensures our quotes are always competitive with the live order book.
        if data.market_best_bid > 0 and data.market_best_ask < 1.0:
            market_mid = (data.market_best_bid + data.market_best_ask) / 2
        else:
            market_mid = fair_value

        # Directional lean: shift center toward fair_value, max ±2¢
        fair_lean = max(-0.02, min(0.02, fair_value - market_mid))

        center = market_mid + fair_lean

        yes_bid = round(max(0.01, center - half_spread - skew), 4)
        yes_ask = round(min(0.99, center + half_spread - skew), 4)
        if yes_bid >= yes_ask:
            yes_bid = round(yes_ask - 0.01, 4)

        no_fair = 1.0 - center
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
    base_spread: float = 0.06
    max_spread: float = 0.12
    volatility_multiplier: float = 5.0

    # Inventory settings
    max_inventory: float = 100.0
    skew_factor: float = 0.0
    quote_size: float = 5.0

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

    # Fee modeling — Polymarket dynamic fees (March 2026)
    market_category: str = "crypto"     # Fee category (crypto peaks at 1.8% taker)
    maker_fill_fraction: float = 0.80   # 80% of fills are maker (resting order hit)
    include_gas: bool = True            # Include Polygon gas cost per fill


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
    total_fees_paid: float = 0.0       # Total taker fees across all fills
    total_rebates_earned: float = 0.0  # Total maker rebates earned
    total_gas_cost: float = 0.0        # Total gas across all txs
    net_fee_impact: float = 0.0        # rebates - fees - gas


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
        total_fees = 0.0
        total_rebates = 0.0
        total_gas = 0.0

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

                    # Apply fee/rebate on bid fill
                    is_maker = random.random() < config.maker_fill_fraction
                    fill_fee = net_fill_fee(
                        buy_price, quotes["size"], is_maker=is_maker,
                        category=config.market_category,
                        include_gas=config.include_gas,
                    )
                    running_pnl -= fill_fee  # negative fill_fee = rebate (adds to pnl)
                    if is_maker:
                        total_rebates += polymarket_maker_rebate_amount(
                            buy_price, quotes["size"], config.market_category)
                    else:
                        total_fees += polymarket_taker_fee_amount(
                            buy_price, quotes["size"], config.market_category)
                    if config.include_gas:
                        total_gas += GAS_COST_PER_TX

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

                    # Apply fee/rebate on ask fill
                    is_maker = random.random() < config.maker_fill_fraction
                    fill_fee = net_fill_fee(
                        sell_price, quotes["size"], is_maker=is_maker,
                        category=config.market_category,
                        include_gas=config.include_gas,
                    )

                    # Match with earliest buy for round-trip P&L
                    buy_price = buy_fills.pop(0)
                    trip_pnl = (sell_price - buy_price) * quotes["size"] - fill_fee
                    spread_pnl += trip_pnl
                    running_pnl += trip_pnl
                    round_trips += 1
                    if is_maker:
                        total_rebates += polymarket_maker_rebate_amount(
                            sell_price, quotes["size"], config.market_category)
                    else:
                        total_fees += polymarket_taker_fee_amount(
                            sell_price, quotes["size"], config.market_category)
                    if config.include_gas:
                        total_gas += GAS_COST_PER_TX

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

        net_fee = total_rebates - total_fees - total_gas

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
            total_fees_paid=round(total_fees, 4),
            total_rebates_earned=round(total_rebates, 4),
            total_gas_cost=round(total_gas, 4),
            net_fee_impact=round(net_fee, 4),
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
        # Adverse selection decreases with wider spread: tight quotes attract informed flow.
        # Empirical estimates for binary prediction markets:
        #   4¢ spread → informed traders often pick off tight quotes → 35% adverse
        #   6¢ spread → balanced                                    → 25% adverse
        #   8-10¢ spread → mostly noise traders cross wide quotes   → 15% adverse
        SPREAD_ADVERSE_MAP = {
            0.04: 0.35,
            0.05: 0.30,
            0.06: 0.25,
            0.07: 0.20,
            0.08: 0.15,
            0.10: 0.15,
        }
        for spread in [0.04, 0.05, 0.06, 0.07, 0.08, 0.10]:
            adv = SPREAD_ADVERSE_MAP.get(spread, 0.25)
            cfg = BacktestConfig(base_spread=spread, adverse_fill_pct=adv)
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
        except Exception as e:
            logger.warning(f"Failed to write signal snapshot to {self.recording_file}: {e}")

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
    print(f"   Fees paid: ${best.total_fees_paid:.4f} | Rebates: ${best.total_rebates_earned:.4f} | Gas: ${best.total_gas_cost:.4f}")
    print(f"   Net fee impact: ${best.net_fee_impact:+.4f}")
    print(f"   Max Inventory: {best.max_inventory:.0f} shares")
    print(f"{'='*70}")

    # Save results
    Path("data").mkdir(exist_ok=True)
    with open("data/backtest_results.json", "w") as f:
        json.dump([asdict(r) for r in results], f, indent=2)
    print(f"\n📁 Results saved to data/backtest_results.json")


async def run_signal_monitor():
    """
    Live signal monitoring mode — connects to Hyperliquid, Binance, and
    Polymarket Gamma API to print real-time signals feeding EnhancedQuoteEngine.

    Use: python mm_enhanced_1.py --signals
    """
    if not HAS_HL_FEED:
        print("ERROR: hyperliquid_api.py not found.")
        print("Make sure hyperliquid_api.py is in the same directory.")
        return

    hl_feed = HyperliquidFeed(poll_interval=3.0)
    fair_value_engine = EnhancedFairValueEngine()
    quote_engine = EnhancedQuoteEngine()
    data_loader = HistoricalDataLoader()

    # Optional feeds — degrade gracefully if files missing
    btc_feed = BinanceBTCFeed() if HAS_BINANCE_FEED else None
    gamma_feed = PolymarketGammaFeed() if HAS_GAMMA_FEED else None
    chainlink_feed = ChainlinkBTCFeed() if HAS_CHAINLINK_FEED else None

    print("""
╔══════════════════════════════════════════════════════════════╗
║  LIVE SIGNAL MONITOR                                         ║
║  Hyperliquid  +  Binance BTC  +  Chainlink  +  Gamma API     ║
║  Press Ctrl+C to stop                                        ║
╚══════════════════════════════════════════════════════════════╝
    """)

    # Start all feeds concurrently
    start_tasks = [hl_feed.start()]
    if btc_feed:
        start_tasks.append(btc_feed.start())
    if gamma_feed:
        start_tasks.append(gamma_feed.start())
    if chainlink_feed:
        start_tasks.append(chainlink_feed.start())
    await asyncio.gather(*start_tasks)

    if btc_feed:
        print(f"  Binance BTC feed:  {'connected' if btc_feed.is_connected else 'connecting...'}")
    else:
        print("  Binance BTC feed:  NOT AVAILABLE (binance_feed.py missing)")
    if chainlink_feed:
        print(f"  Chainlink feed:    polling Polygon (settlement source)")
    else:
        print("  Chainlink feed:    NOT AVAILABLE (chainlink_feed.py missing)")
    if gamma_feed:
        print(f"  Polymarket Gamma:  polling active BTC markets")
    else:
        print("  Polymarket Gamma:  NOT AVAILABLE (polymarket_gamma.py missing)")

    # Brief wait for WebSocket connections to settle
    await asyncio.sleep(5)

    try:
        cycle = 0
        while True:
            hl_fields = hl_feed.get_snapshot_fields()
            btc_fields = btc_feed.get_snapshot_fields() if btc_feed else {}
            gamma_fields = gamma_feed.get_snapshot_fields() if gamma_feed else {}
            cl_fields = chainlink_feed.get_snapshot_fields() if chainlink_feed else {}

            # Wire Binance price into Chainlink feed for lead calculation
            if chainlink_feed and btc_feed and btc_feed.price:
                chainlink_feed.binance_price = btc_feed.price

            snapshot = SideDataSnapshot(
                btc_price=btc_fields.get("btc_price") or hl_fields["hl_oracle_price"],
                btc_change_1m=btc_fields.get("btc_change_1m", 0.0),
                btc_change_5m=btc_fields.get("btc_change_5m", 0.0),
                btc_volatility_1m=btc_fields.get("btc_volatility_1m", 0.001),
                hl_oracle_price=hl_fields["hl_oracle_price"],
                hl_funding_rate=hl_fields["hl_funding_rate"],
                hl_open_interest=hl_fields["hl_open_interest"],
                cvd_signal=hl_fields["cvd_signal"],
                liq_signal=hl_fields["liq_signal"],
                funding_signal=hl_fields["funding_signal"],
                oi_signal=hl_fields["oi_signal"],
                chainlink_price=cl_fields.get("chainlink_price", 0.0),
                market_spread=gamma_fields.get("market_spread", 0.0),
                # ← FIXED: capture actual YES bid/ask every tick
                market_best_bid=gamma_feed.best_bid if gamma_feed else 0.0,
                market_best_ask=gamma_feed.best_ask if gamma_feed else 1.0,
                seconds_to_expiry=gamma_fields.get("seconds_to_expiry", 300.0),
                timestamp=time.time(),
            )

            # Record every single tick — no skipping
            data_loader.record_tick(snapshot)

            # Compute quotes
            fair_value = fair_value_engine.estimate(snapshot, market_yes_price=0.50)
            quotes = quote_engine.generate_quotes(fair_value, net_inventory=0.0, data=snapshot)

            cycle += 1
            if cycle % 2 == 0:  # Print every ~2 seconds
                hl_status = hl_feed.status()
                print(f"\n{'─'*65}")
                # BTC price line — show Binance if available
                if btc_feed and btc_feed.price:
                    print(
                        f"  BTC Binance:   ${btc_feed.price:,.2f} "
                        f"({btc_feed.change_1m*100:+.3f}% 1m | {btc_feed.change_5m*100:+.3f}% 5m)"
                    )
                if chainlink_feed and chainlink_feed.price:
                    lead = chainlink_feed.binance_lead
                    lead_str = f"Binance lead: ${lead:+.2f}" if chainlink_feed.binance_price else "no Binance ref"
                    print(
                        f"  BTC Chainlink: ${chainlink_feed.price:,.2f}  ({lead_str})  "
                        f"[settlement source — age {chainlink_feed.age:.0f}s]"
                    )
                print(
                    f"  BTC Oracle:    ${hl_status['oracle_price']:,.2f}  |  "
                    f"Funding: {hl_status['funding_rate']}/hr"
                )
                print(
                    f"  OI: ${float(hl_status['open_interest'].replace(',','')):,.0f}  |  "
                    f"WS Trades: {hl_status['ws_trade_count']}"
                )
                if gamma_feed:
                    gstatus = gamma_feed.status()
                    print(
                        f"  Gamma Market: bid={gstatus['best_bid']}  ask={gstatus['best_ask']}  "
                        f"spread={gstatus['spread']}  expiry={gstatus['seconds_to_expiry']}"
                    )
                print(f"  ┌─ Signals ──────────────────────────────────────┐")
                print(f"  │  CVD:      {hl_status['cvd_signal']:>7s}  (buy vs sell flow)      │")
                print(f"  │  Liq:      {hl_status['liq_signal']:>7s}  (liquidation pressure)  │")
                print(f"  │  Funding:  {hl_status['funding_signal']:>7s}  (crowd positioning)    │")
                print(f"  │  OI:       {hl_status['oi_signal']:>7s}  (new money flow)        │")
                print(f"  └────────────────────────────────────────────────┘")
                print(f"  Fair Value: {quotes['fair_value']:.4f}  |  Spread: {quotes['spread']:.4f}")
                print(f"  YES Bid/Ask: {quotes['yes_bid']:.4f} / {quotes['yes_ask']:.4f}")
                print(f"  Adjustments: {quotes['adjustments']}")

            await asyncio.sleep(1)

    except KeyboardInterrupt:
        print("\nStopping signal monitor...")
    finally:
        stop_tasks = [hl_feed.stop()]
        if btc_feed:
            stop_tasks.append(btc_feed.stop())
        if gamma_feed:
            stop_tasks.append(gamma_feed.stop())
        await asyncio.gather(*stop_tasks)
        print("Signal monitor stopped. Recorded ticks saved to data/mm_historical.jsonl")


async def run_paper_trader():
    """
    Autonomous paper trading loop.

    Connects all feeds, generates quotes every second, simulates fills
    against live Polymarket prices, and writes state to
    data/paper_mm_state.json for the dashboard to read.

    Run the dashboard in a separate terminal:
        venv/bin/python3 mm_dashboard.py
      or:
        venv/bin/python3 mm_dashboard.py --web
    """
    if not HAS_HL_FEED:
        print("ERROR: hyperliquid_api.py not found.")
        return

    hl_feed = HyperliquidFeed(poll_interval=3.0)
    btc_feed = BinanceBTCFeed() if HAS_BINANCE_FEED else None
    gamma_feed = PolymarketGammaFeed() if HAS_GAMMA_FEED else None
    chainlink_feed = ChainlinkBTCFeed() if HAS_CHAINLINK_FEED else None

    fair_value_engine = EnhancedFairValueEngine()
    quote_engine = EnhancedQuoteEngine()
    data_loader = HistoricalDataLoader()
    confidence_calc = ConfidenceCalculator(max_inventory=15.0)
    alerter = AlertManager.from_env()
    if alerter.enabled:
        logger.info("AlertManager: Telegram/Discord alerting active")

    trader = PaperTrader(starting_capital=50.0, max_inventory=25.0, base_quote_size=5.0)
    trader.load()

    print("""
╔══════════════════════════════════════════════════════════════╗
║  PAPER TRADER                                                ║
║  Autonomous market making — no real funds at risk            ║
║                                                              ║
║  Dashboard (separate terminal):                              ║
║    venv/bin/python3 mm_dashboard.py                          ║
║    venv/bin/python3 mm_dashboard.py --web   (browser UI)     ║
║                                                              ║
║  Press Ctrl+C to stop and save state                         ║
╚══════════════════════════════════════════════════════════════╝
    """)

    start_tasks = [hl_feed.start()]
    if btc_feed:
        start_tasks.append(btc_feed.start())
    if gamma_feed:
        start_tasks.append(gamma_feed.start())
    if chainlink_feed:
        start_tasks.append(chainlink_feed.start())
    await asyncio.gather(*start_tasks)
    await asyncio.sleep(5)  # Let feeds settle

    # Reconcile any inventory loaded from disk against the live market
    if gamma_feed:
        trader.reconcile_inventory(
            gamma_feed._condition_id,
            gamma_feed.seconds_to_expiry,
        )

    logger.info("Paper trader started")

    if alerter.enabled:
        await alerter.send(
            f"🟢 <b>Paper trader started</b>\n"
            f"Capital: ${trader.state.starting_capital:.2f}  "
            f"Loaded PnL: {trader.state.realized_pnl:+.2f}"
        )

    # ── Startup blackout ──────────────────────────────────────────────────────
    # Don't quote for the first 5 minutes (300s). During warmup:
    #   - btc_change_5m is 0 (no 5-min history yet)
    #   - liq_signal is 0 (needs 500 HL trades for rolling avg)
    #   - oi_signal is near 0 (needs 15-min OI history)
    #   - signal_agreement falls back to 85 ("calm market") — artificially high
    # Quoting with FULL confidence on no real data generates bad early fills.
    _WARMUP_SECONDS = 300
    _start_time = time.time()
    _warmed_up = False
    logger.info(f"Feed warmup: no quotes for {_WARMUP_SECONDS}s while signals stabilize")

    try:
        cycle = 0
        _last_save = time.time()
        _last_summary = time.time()
        _SUMMARY_INTERVAL = 1800  # 30-min P&L digest
        while True:
            hl_fields = hl_feed.get_snapshot_fields()
            btc_fields = btc_feed.get_snapshot_fields() if btc_feed else {}
            gamma_fields = gamma_feed.get_snapshot_fields() if gamma_feed else {}
            cl_fields = chainlink_feed.get_snapshot_fields() if chainlink_feed else {}

            # Wire Binance price into Chainlink feed for lead calculation
            if chainlink_feed and btc_feed and btc_feed.price:
                chainlink_feed.binance_price = btc_feed.price

            snapshot = SideDataSnapshot(
                btc_price=btc_fields.get("btc_price") or hl_fields["hl_oracle_price"],
                btc_change_1m=btc_fields.get("btc_change_1m", 0.0),
                btc_change_5m=btc_fields.get("btc_change_5m", 0.0),
                btc_volatility_1m=btc_fields.get("btc_volatility_1m", 0.001),
                hl_oracle_price=hl_fields["hl_oracle_price"],
                hl_funding_rate=hl_fields["hl_funding_rate"],
                hl_open_interest=hl_fields["hl_open_interest"],
                cvd_signal=hl_fields["cvd_signal"],
                liq_signal=hl_fields["liq_signal"],
                funding_signal=hl_fields["funding_signal"],
                oi_signal=hl_fields["oi_signal"],
                chainlink_price=cl_fields.get("chainlink_price", 0.0),
                market_spread=gamma_fields.get("market_spread", 0.0),
                market_best_bid=gamma_feed.best_bid if gamma_feed else 0.0,
                market_best_ask=gamma_feed.best_ask if gamma_feed else 1.0,
                seconds_to_expiry=gamma_fields.get("seconds_to_expiry", 300.0),
                timestamp=time.time(),
            )

            # Build feed timestamps for freshness scoring
            now = time.time()
            feed_timestamps = {
                "binance": now if (btc_feed and btc_feed.is_connected) else 0.0,
                "hyperliquid": now if hl_feed.is_connected else 0.0,
                "gamma": now if (gamma_feed and gamma_feed.is_fresh) else 0.0,
                "chainlink": (chainlink_feed._last_update if chainlink_feed else 0.0),
            }

            # Use actual live market mid as the price anchor — NOT hardcoded 0.50.
            # The fair value engine blends 60% market weight, so feeding the real
            # mid price is critical for quotes to track where the market actually is.
            if snapshot.market_best_bid > 0 and snapshot.market_best_ask < 1.0:
                actual_market_mid = (snapshot.market_best_bid + snapshot.market_best_ask) / 2
            else:
                actual_market_mid = 0.50

            # High-probability market filter: only quote when market price is in target range.
            # At extreme prices (>0.80 or <0.20), outcome is more predictable, reducing inventory risk.
            # At mid prices (0.40–0.60), market is uncertain and adverse selection is highest.
            # Configurable via .env: QUOTE_MIN_PRICE, QUOTE_MAX_PRICE (defaults to no filtering)
            quote_min = float(os.getenv("QUOTE_MIN_PRICE", "0.0"))
            quote_max = float(os.getenv("QUOTE_MAX_PRICE", "1.0"))
            if not (quote_min <= actual_market_mid <= quote_max):
                await asyncio.sleep(1)
                cycle += 1
                continue

            fair_value = fair_value_engine.estimate(snapshot, market_yes_price=actual_market_mid)
            quotes = quote_engine.generate_quotes(
                fair_value,
                net_inventory=trader.state.net_inventory,
                data=snapshot,
            )

            confidence = confidence_calc.score(
                snapshot,
                net_inventory=trader.state.net_inventory,
                feed_timestamps=feed_timestamps,
                market_yes_price=fair_value,
                consecutive_losses=trader.state.consecutive_losses,
            )

            # Apply confidence spread multiplier: widen bid/ask prices outward
            # from the center so the actual quoted prices reflect the wider spread.
            adjusted_quotes = dict(quotes)
            orig_spread = quotes.get("spread", 0.02)
            new_spread = orig_spread * confidence.spread_multiplier
            extra_half = (new_spread - orig_spread) / 2
            adjusted_quotes["spread"] = new_spread
            adjusted_quotes["yes_bid"] = round(max(0.01, quotes["yes_bid"] - extra_half), 4)
            adjusted_quotes["yes_ask"] = round(min(0.99, quotes["yes_ask"] + extra_half), 4)

            # ── Warmup gate ───────────────────────────────────────────────────
            # Block all fills until feeds have had time to stabilize.
            # A secondary readiness check: btc_change_5m must be non-zero
            # (confirms 5 min of BTC price history is available).
            if not _warmed_up:
                elapsed = time.time() - _start_time
                has_5m_data = abs(snapshot.btc_change_5m) > 0
                if elapsed >= _WARMUP_SECONDS and has_5m_data:
                    _warmed_up = True
                    logger.info(
                        f"Warmup complete ({elapsed:.0f}s). "
                        f"btc_5m={snapshot.btc_change_5m*100:+.3f}%  "
                        f"conf={confidence.score:.0f} ({confidence.tier})"
                    )
                else:
                    remaining = max(0, _WARMUP_SECONDS - elapsed)
                    if cycle % 30 == 0:
                        logger.info(
                            f"Warmup: {remaining:.0f}s remaining  "
                            f"5m_ready={has_5m_data}  conf={confidence.score:.0f}"
                        )
                    age = gamma_feed.price_age if gamma_feed else 0
                    print(
                        f"  YES={snapshot.market_best_bid:.4f}  "
                        f"NO={1-snapshot.market_best_ask:.4f}  "
                        f"spread={snapshot.market_spread:.4f}  "
                        f"expiry={snapshot.seconds_to_expiry:.0f}s  "
                        f"age={age:.1f}s",
                        flush=True,
                    )
                    await asyncio.sleep(1)
                    cycle += 1
                    continue

            # Simulate fills
            market_id = gamma_feed._condition_id or "" if gamma_feed else ""
            fills = trader.process_cycle(adjusted_quotes, snapshot, confidence, market_id)

            if fills:
                for fill in fills:
                    logger.info(f"FILL: {fill.side} {fill.size:.0f}sh @ {fill.price:.4f}")
                    if alerter.enabled:
                        side_label = fill.side.replace("_", " ").upper()
                        emoji = "🟢" if fill.side == "buy_yes" else "🔴"
                        message = (
                            f"{emoji} <b>Paper fill</b> {side_label}\n"
                            f"Size: {fill.size:.0f}sh  Price: {fill.price*100:.1f}c\n"
                            f"Inventory: {trader.state.net_inventory:+.0f}sh  "
                            f"Total fills: {trader.state.total_fills}"
                        )
                        if fill.pnl != 0:
                            trip_emoji = "✅" if fill.pnl > 0 else "❌"
                            message += (
                                f"\n{trip_emoji} Closed PnL: {fill.pnl:+.4f}  "
                                f"Total: {trader.state.realized_pnl:+.2f}\n"
                                f"Trips: {trader.state.round_trips}  Win: "
                                f"{trader.state.winning_trips/max(1,trader.state.round_trips)*100:.0f}%"
                            )
                        await alerter.send(
                            message,
                        )

            # Persist state after every fill, and at least every 30s (crash safety)
            _now = time.time()
            if fills or (_now - _last_save >= 30):
                trader.save()
                _last_save = _now

            # ── Alert checks ─────────────────────────────────────────────────
            if alerter.enabled:
                # Circuit breaker: confidence PAUSED due to loss streak
                if confidence.tier == "PAUSED" and trader.state.consecutive_losses >= 5:
                    await alerter.send(
                        f"⚠️ <b>CIRCUIT BREAKER</b>\n"
                        f"Confidence PAUSED after {trader.state.consecutive_losses} "
                        f"consecutive losses.\n"
                        f"PnL: {trader.state.realized_pnl:+.2f}  "
                        f"Drawdown: {trader.state.max_drawdown:.2f}",
                        key="circuit_breaker",
                        cooldown=600,
                    )
                # Drawdown threshold (5% of starting capital)
                _dd_threshold = trader.state.starting_capital * 0.05
                if trader.state.max_drawdown >= _dd_threshold:
                    await alerter.send(
                        f"⚠️ <b>DRAWDOWN ALERT</b>\n"
                        f"Max drawdown ${trader.state.max_drawdown:.2f} exceeded "
                        f"${_dd_threshold:.2f} threshold.\n"
                        f"Cash: ${trader.state.cash:.2f}  "
                        f"PnL: {trader.state.realized_pnl:+.2f}",
                        key="drawdown",
                        cooldown=1800,
                    )
                # Chainlink stale
                if chainlink_feed and chainlink_feed.age > 120:
                    await alerter.send(
                        f"⚠️ <b>CHAINLINK STALE</b>\n"
                        f"No Chainlink update for {chainlink_feed.age:.0f}s "
                        f"(settlement source!).\nLast price: "
                        f"${chainlink_feed.price:,.2f}",
                        key="chainlink_stale",
                        cooldown=600,
                    )
                # Gamma API cannot find a market
                if gamma_feed and gamma_feed._slug_miss_count >= 3:
                    await alerter.send(
                        f"⚠️ <b>GAMMA MARKET NOT FOUND</b>\n"
                        f"{gamma_feed._slug_miss_count} consecutive window misses.\n"
                        f"Last slug: {gamma_feed._last_slug or 'none'}\n"
                        f"Bot is running without live Polymarket bid/ask.",
                        key="gamma_miss",
                        cooldown=600,
                    )
                # 30-min P&L digest
                if _now - _last_summary >= _SUMMARY_INTERVAL:
                    _last_summary = _now
                    wr = (trader.state.winning_trips /
                          max(1, trader.state.round_trips) * 100)
                    await alerter.send(
                        f"📊 <b>30-min digest</b>\n"
                        f"PnL: {trader.state.realized_pnl:+.2f}  "
                        f"Cash: ${trader.state.cash:.2f}\n"
                        f"Fills: {trader.state.total_fills}  "
                        f"Trips: {trader.state.round_trips}  Win: {wr:.0f}%\n"
                        f"Inventory: {trader.state.net_inventory:+.0f}sh  "
                        f"MaxDD: ${trader.state.max_drawdown:.2f}",
                        key="summary",
                    )
            # ── End alert checks ──────────────────────────────────────────────

            # Write enriched state for dashboard
            state_dict = {
                **{k: v for k, v in trader.state.__dict__.items()
                   if not k.startswith("_")},
                # Add live signal fields for dashboard display
                "btc_price": snapshot.btc_price,
                "btc_change_1m": snapshot.btc_change_1m,
                "btc_change_5m": snapshot.btc_change_5m,
                "btc_volatility_1m": snapshot.btc_volatility_1m,
                "cvd_signal": snapshot.cvd_signal,
                "funding_signal": snapshot.funding_signal,
                "liq_signal": snapshot.liq_signal,
                "oi_signal": snapshot.oi_signal,
                "confidence_tier": confidence.tier,
                "confidence_reason": confidence.reason,
                "confidence_signal_agreement": confidence.signal_agreement,
                "confidence_data_freshness": confidence.data_freshness,
                "confidence_spread_health": confidence.spread_health,
                "confidence_inventory_neutral": confidence.inventory_neutral,
            }
            try:
                _state_path = Path(__file__).resolve().parent / "data" / "paper_mm_state.json"
                _state_path.parent.mkdir(exist_ok=True)
                with open(_state_path, "w") as f:
                    json.dump(state_dict, f)
            except Exception as e:
                logger.warning(f"State write failed: {e}")

            # Record tick for backtesting
            data_loader.record_tick(snapshot)

            cycle += 1
            s = trader.state
            print(
                f"  YES={snapshot.market_best_bid:.4f}  "
                f"NO={1-snapshot.market_best_ask:.4f}  "
                f"spread={snapshot.market_spread:.4f}  "
                f"expiry={snapshot.seconds_to_expiry:.0f}s  "
                f"pnl={s.realized_pnl:+.2f}  inv={s.net_inventory:+.0f}  "
                f"conf={confidence.tier}",
                flush=True,
            )
            if cycle % 10 == 0:  # Full summary every 10s
                logger.info(
                    f"cycle={cycle} fills={s.total_fills} trips={s.round_trips} "
                    f"conf={confidence.score:.0f}%[{confidence.tier}]"
                )

            await asyncio.sleep(1)

    except KeyboardInterrupt:
        print("\nStopping paper trader...")
    finally:
        trader.save()
        stop_tasks = [hl_feed.stop()]
        if btc_feed:
            stop_tasks.append(btc_feed.stop())
        if gamma_feed:
            stop_tasks.append(gamma_feed.stop())
        if chainlink_feed:
            stop_tasks.append(chainlink_feed.stop())
        await asyncio.gather(*stop_tasks)
        s = trader.state
        print(f"\nFinal P&L: {s.realized_pnl:+.2f}  |  Fills: {s.total_fills}  |  Trips: {s.round_trips}")
        print("State saved to data/paper_mm_state.json")


async def run_live_trader(dual: bool = False):
    """
    Autonomous live trading loop — posts real limit orders to Polymarket's CLOB.

    Mirrors run_paper_trader() structurally.  The only differences are:
      - Uses LiveOrderManager instead of PaperTrader
      - Reads CLOB credentials from environment
      - SharedPositionLock is activated when dual=True
      - cancel_all() is called on exit so no orphaned orders remain

    WARNING: This uses real funds on Polygon Mainnet.
    """
    if not HAS_LIVE_TRADER:
        print("ERROR: live_order_manager.py not found or py-clob-client not installed.")
        print("       pip install py-clob-client eth-account websockets")
        return

    if not HAS_HL_FEED:
        print("ERROR: hyperliquid_api.py not found.")
        return

    private_key   = os.environ.get("POLYMARKET_PRIVATE_KEY", "")
    api_key       = os.environ.get("POLYMARKET_API_KEY", "")
    api_secret    = os.environ.get("POLYMARKET_API_SECRET", "")
    api_passphrase = os.environ.get("POLYMARKET_API_PASSPHRASE", "")

    missing = [k for k, v in {
        "POLYMARKET_PRIVATE_KEY": private_key,
        "POLYMARKET_API_KEY": api_key,
        "POLYMARKET_API_SECRET": api_secret,
        "POLYMARKET_API_PASSPHRASE": api_passphrase,
    }.items() if not v]
    if missing:
        print(f"ERROR: missing env vars: {', '.join(missing)}")
        print("       Set them in your .env file and re-run.")
        return

    hl_feed       = HyperliquidFeed(poll_interval=3.0)
    btc_feed      = BinanceBTCFeed()      if HAS_BINANCE_FEED   else None
    gamma_feed    = PolymarketGammaFeed() if HAS_GAMMA_FEED     else None
    chainlink_feed = ChainlinkBTCFeed()  if HAS_CHAINLINK_FEED  else None

    fair_value_engine = EnhancedFairValueEngine()
    quote_engine      = EnhancedQuoteEngine()
    data_loader       = HistoricalDataLoader()
    confidence_calc   = ConfidenceCalculator(max_inventory=5.0)

    position_lock = SharedPositionLock() if dual else None

    alerter = AlertManager.from_env()
    if alerter.enabled:
        logger.info("AlertManager: Telegram/Discord alerting active")

    manager = LiveOrderManager(private_key, api_key, api_secret, api_passphrase)

    print("""
\033[31m╔══════════════════════════════════════════════════════════════╗
║  LIVE TRADER — REAL FUNDS ON POLYGON MAINNET                 ║
║  Orders are posted to clob.polymarket.com                    ║
║                                                              ║
║  Press Ctrl+C to stop (open orders will be cancelled)        ║
╚══════════════════════════════════════════════════════════════╝\033[0m
    """)
    if dual:
        print("  Dual mode: SharedPositionLock active\n")

    start_tasks = [hl_feed.start()]
    if btc_feed:
        start_tasks.append(btc_feed.start())
    if gamma_feed:
        start_tasks.append(gamma_feed.start())
    if chainlink_feed:
        start_tasks.append(chainlink_feed.start())
    await asyncio.gather(*start_tasks)
    await asyncio.sleep(5)  # Let feeds settle

    await manager.start()
    logger.info("Live trader started")

    # Simple inventory / P&L tracking (fills come from LiveOrderManager)
    total_fills = 0
    realized_pnl = 0.0
    net_inventory = 0.0
    consecutive_losses = 0
    max_live_inventory = confidence_calc.max_inventory
    max_live_loss = float(os.environ.get("MAX_LIVE_LOSS", "50.0"))

    try:
        cycle = 0
        while True:
            hl_fields    = hl_feed.get_snapshot_fields()
            btc_fields   = btc_feed.get_snapshot_fields()   if btc_feed        else {}
            gamma_fields = gamma_feed.get_snapshot_fields() if gamma_feed      else {}
            cl_fields    = chainlink_feed.get_snapshot_fields() if chainlink_feed else {}

            if chainlink_feed and btc_feed and btc_feed.price:
                chainlink_feed.binance_price = btc_feed.price

            snapshot = SideDataSnapshot(
                btc_price=btc_fields.get("btc_price") or hl_fields["hl_oracle_price"],
                btc_change_1m=btc_fields.get("btc_change_1m", 0.0),
                btc_change_5m=btc_fields.get("btc_change_5m", 0.0),
                btc_volatility_1m=btc_fields.get("btc_volatility_1m", 0.001),
                hl_oracle_price=hl_fields["hl_oracle_price"],
                hl_funding_rate=hl_fields["hl_funding_rate"],
                hl_open_interest=hl_fields["hl_open_interest"],
                cvd_signal=hl_fields["cvd_signal"],
                liq_signal=hl_fields["liq_signal"],
                funding_signal=hl_fields["funding_signal"],
                oi_signal=hl_fields["oi_signal"],
                chainlink_price=cl_fields.get("chainlink_price", 0.0),
                market_spread=gamma_fields.get("market_spread", 0.0),
                market_best_bid=gamma_feed.best_bid if gamma_feed else 0.0,
                market_best_ask=gamma_feed.best_ask if gamma_feed else 1.0,
                seconds_to_expiry=gamma_fields.get("seconds_to_expiry", 300.0),
                timestamp=time.time(),
            )

            now = time.time()
            feed_timestamps = {
                "binance":     now if (btc_feed and btc_feed.is_connected)               else 0.0,
                "hyperliquid": now if hl_feed.is_connected                               else 0.0,
                "gamma":       now if (gamma_feed and gamma_feed.is_fresh)               else 0.0,
                "chainlink":   (chainlink_feed._last_update if chainlink_feed else 0.0),
            }

            fair_value = fair_value_engine.estimate(snapshot, market_yes_price=0.50)
            quotes = quote_engine.generate_quotes(
                fair_value,
                net_inventory=net_inventory,
                data=snapshot,
            )

            confidence = confidence_calc.score(
                snapshot,
                net_inventory=net_inventory,
                feed_timestamps=feed_timestamps,
                market_yes_price=fair_value,
                consecutive_losses=consecutive_losses,
            )

            adjusted_quotes = dict(quotes)
            adjusted_quotes["spread"] = quotes.get("spread", 0.02) * confidence.spread_multiplier

            yes_token_id = gamma_feed.yes_token_id if gamma_feed else None
            market_id    = gamma_feed._condition_id or "" if gamma_feed else ""

            # Hard position limit: halt quoting when inventory is maxed
            skip = False
            if abs(net_inventory) >= max_live_inventory:
                logger.warning(
                    f"Hard position limit: inv={net_inventory:+.0f} >= {max_live_inventory:.0f}, "
                    f"cancelling quotes this cycle"
                )
                skip = True

            # Dual mode: skip if the position lock is held by the directional bot
            if not skip and position_lock and not position_lock.acquire_mm(market_id):
                skip = True

            if skip:
                fills = []
            else:
                fills = await manager.process_cycle(
                    adjusted_quotes, snapshot, confidence, market_id, yes_token_id
                )
                if position_lock:
                    position_lock.release_mm(market_id)

            for fill in fills:
                total_fills += 1
                fee = net_fill_fee(fill.price, fill.size, is_maker=True)
                if fill.pnl < 0:
                    consecutive_losses += 1
                elif fill.pnl > 0:
                    consecutive_losses = 0
                if fill.side == "sell_yes":
                    realized_pnl += fill.pnl - fee
                    net_inventory -= fill.size
                else:
                    realized_pnl -= fee   # entry fill: fee cost hits P&L immediately
                    net_inventory += fill.size
                logger.info(
                    f"LIVE FILL: {fill.side} {fill.size:.0f}sh @ {fill.price:.4f} "
                    f"pnl={fill.pnl:+.4f}  fee={fee:+.4f}"
                )

            # Drawdown circuit breaker: stop if session loss exceeds limit
            if realized_pnl < -max_live_loss:
                logger.error(
                    f"CIRCUIT BREAKER: session loss {realized_pnl:+.2f} exceeds "
                    f"-{max_live_loss:.2f} limit. Cancelling all orders and stopping."
                )
                await alerter.send(
                    f"🚨 <b>LIVE CIRCUIT BREAKER FIRED</b>\n"
                    f"Session loss <b>{realized_pnl:+.2f}</b> exceeded "
                    f"-{max_live_loss:.2f} limit.\n"
                    f"All orders cancelled. Bot stopped.",
                    key="live_circuit_breaker",
                )
                break

            # ── Alert checks (live) ───────────────────────────────────────────
            if alerter.enabled:
                # Chainlink stale
                if chainlink_feed and chainlink_feed.age > 120:
                    await alerter.send(
                        f"⚠️ <b>CHAINLINK STALE (LIVE)</b>\n"
                        f"No Chainlink update for {chainlink_feed.age:.0f}s "
                        f"(settlement source!).\nLast price: "
                        f"${chainlink_feed.price:,.2f}",
                        key="live_chainlink_stale",
                        cooldown=600,
                    )
                # Gamma API cannot find a market
                if gamma_feed and gamma_feed._slug_miss_count >= 3:
                    await alerter.send(
                        f"⚠️ <b>GAMMA MARKET NOT FOUND (LIVE)</b>\n"
                        f"{gamma_feed._slug_miss_count} consecutive window misses.\n"
                        f"Last slug: {gamma_feed._last_slug or 'none'}\n"
                        f"Bot is running without live Polymarket bid/ask.",
                        key="live_gamma_miss",
                        cooldown=600,
                    )
            # ── End alert checks ──────────────────────────────────────────────

            # Write state for dashboard
            state_dict = {
                "mode": "live",
                "dual": dual,
                "btc_price": snapshot.btc_price,
                "btc_change_1m": snapshot.btc_change_1m,
                "btc_change_5m": snapshot.btc_change_5m,
                "btc_volatility_1m": snapshot.btc_volatility_1m,
                "cvd_signal": snapshot.cvd_signal,
                "funding_signal": snapshot.funding_signal,
                "liq_signal": snapshot.liq_signal,
                "oi_signal": snapshot.oi_signal,
                "yes_bid": adjusted_quotes.get("yes_bid", 0.0),
                "yes_ask": adjusted_quotes.get("yes_ask", 1.0),
                "fair_value": fair_value,
                "spread": adjusted_quotes.get("spread", 0.0),
                "confidence_tier": confidence.tier,
                "confidence_score": confidence.score,
                "confidence_reason": confidence.reason,
                "net_inventory": net_inventory,
                "realized_pnl": realized_pnl,
                "total_fills": total_fills,
                "market_id": market_id,
                "yes_token_id": yes_token_id or "",
                "seconds_to_expiry": snapshot.seconds_to_expiry,
                "ws_connected": manager.is_connected,
                "open_quotes": manager.status()["open_quotes"],
                "last_update": now,
            }
            try:
                _state_path = Path(__file__).resolve().parent / "data" / "live_mm_state.json"
                _state_path.parent.mkdir(exist_ok=True)
                with open(_state_path, "w") as f:
                    json.dump(state_dict, f)
            except Exception as e:
                logger.warning(f"State write failed: {e}")

            data_loader.record_tick(snapshot)

            cycle += 1
            if cycle % 10 == 0:
                logger.info(
                    f"cycle={cycle} "
                    f"pnl={realized_pnl:+.4f} "
                    f"inv={net_inventory:+.0f} "
                    f"conf={confidence.score:.0f}%[{confidence.tier}] "
                    f"fills={total_fills} "
                    f"ws={'✓' if manager.is_connected else '✗'}"
                )

            await asyncio.sleep(1)

    except KeyboardInterrupt:
        print("\nStopping live trader...")
    finally:
        await manager.stop()
        stop_tasks = [hl_feed.stop()]
        if btc_feed:
            stop_tasks.append(btc_feed.stop())
        if gamma_feed:
            stop_tasks.append(gamma_feed.stop())
        if chainlink_feed:
            stop_tasks.append(chainlink_feed.stop())
        await asyncio.gather(*stop_tasks)
        print(f"\nFinal P&L: {realized_pnl:+.4f}  |  Fills: {total_fills}")
        print("Open orders cancelled. State saved to data/live_mm_state.json")


# ═══════════════════════════════════════════════════════════════════════════════
# REAL DATA BACKTEST: Uses mm_historical.jsonl instead of synthetic data
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class RealBacktestConfig:
    """Configuration for real data backtest."""
    min_yes_price: float = 0.0      # Only quote when market price >= this
    max_yes_price: float = 1.0      # Only quote when market price <= this
    max_tte: float = 300.0          # Max time to expiry (skip if > this)
    base_spread: float = 0.06
    max_spread: float = 0.12
    quote_size: float = 1.0
    max_inventory: float = 15.0


class RealDataBacktester:
    """Replay mm_historical.jsonl with realistic fill simulation."""

    def __init__(self):
        self.fv_engine = EnhancedFairValueEngine()
        self.quote_engine = EnhancedQuoteEngine()

    def run(self, config: RealBacktestConfig):
        """Run backtest against real historical data."""
        import json

        # Load real data
        rows = []
        try:
            with open("data/mm_historical.jsonl") as f:
                for line in f:
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        except FileNotFoundError:
            print("ERROR: data/mm_historical.jsonl not found")
            return

        # Filter: only rows with valid orderbook data
        valid_rows = [
            r for r in rows
            if r.get("market_best_bid", 0) > 0 and r.get("market_best_ask", 1) < 1.0
            and r.get("seconds_to_expiry", 0) > 0
        ]
        print(f"\nLoaded {len(rows)} rows, {len(valid_rows)} with valid orderbook")

        # Group into market sessions (when seconds_to_expiry resets)
        sessions = []
        current_session = []
        last_tte = valid_rows[0].get("seconds_to_expiry", 300) if valid_rows else 300

        for row in valid_rows:
            tte = row.get("seconds_to_expiry", 300)
            if tte > last_tte + 10:  # Reset detected (new market window)
                if current_session:
                    sessions.append(current_session)
                current_session = []
            current_session.append(row)
            last_tte = tte

        if current_session:
            sessions.append(current_session)

        print(f"Grouped into {len(sessions)} market sessions\n")

        # Run backtest per session
        total_fills = 0
        total_round_trips = 0
        winning_trips = 0
        total_pnl = 0.0
        max_inventory = 0.0
        max_drawdown = 0.0
        equity = config.quote_size * 100  # Start with ~$100 virtual capital
        peak_equity = equity
        debug_count = 0  # Show first 5 quotes for debugging

        for session_idx, session in enumerate(sessions):
            cash = equity
            inventory = 0.0
            avg_entry = 0.0
            session_fills = 0

            for row in session:
                market_mid = (row["market_best_bid"] + row["market_best_ask"]) / 2

                # Filter by price range
                if not (config.min_yes_price <= market_mid <= config.max_yes_price):
                    continue

                # Build snapshot from row
                snapshot = SideDataSnapshot(
                    btc_price=row.get("btc_price", 0.5),
                    btc_change_1m=row.get("btc_change_1m", 0.0),
                    btc_change_5m=row.get("btc_change_5m", 0.0),
                    btc_volatility_1m=row.get("btc_volatility_1m", 0.001),
                    hl_oracle_price=row.get("hl_oracle_price", 0.5),
                    hl_funding_rate=row.get("hl_funding_rate", 0.0),
                    hl_open_interest=row.get("hl_open_interest", 0.0),
                    cvd_signal=row.get("cvd_signal", 0.0),
                    liq_signal=row.get("liq_signal", 0.0),
                    funding_signal=row.get("funding_signal", 0.0),
                    oi_signal=row.get("oi_signal", 0.0),
                    chainlink_price=row.get("chainlink_price", 0.0),
                    market_spread=row.get("market_spread", 0.01),
                    market_best_bid=row["market_best_bid"],
                    market_best_ask=row["market_best_ask"],
                    seconds_to_expiry=row.get("seconds_to_expiry", 300.0),
                    timestamp=row.get("timestamp", 0.0),
                )

                # Generate quotes — quote INSIDE the market spread
                # (realistic market maker behavior: capture the bid-ask spread)
                # Post bid: 0.5¢ inside the market bid
                # Post ask: 0.5¢ inside the market ask
                our_bid = round(snapshot.market_best_bid + 0.005, 4)
                our_ask = round(snapshot.market_best_ask - 0.005, 4)

                # Ensure bid < ask
                if our_bid >= our_ask:
                    our_bid = round(snapshot.market_best_bid, 4)
                    our_ask = round(snapshot.market_best_ask, 4)

                # Debug: show first few ticks
                if debug_count < 5:
                    print(f"  Tick {debug_count}: market_bid={snapshot.market_best_bid:.4f} market_ask={snapshot.market_best_ask:.4f}  "
                          f"→ our_bid={our_bid:.4f} our_ask={our_ask:.4f}  "
                          f"mid={market_mid:.4f}")
                    debug_count += 1

                # Simulate fills — if we're inside/at the market, we might get filled
                # Bid fill probability: 15% per tick if we're at or inside the bid
                if our_bid >= snapshot.market_best_bid - 0.001 and random.random() < 0.15:
                    fill_size = config.quote_size
                    cost = fill_size * our_bid
                    if cost <= cash:
                        fee = net_fill_fee(our_bid, fill_size, is_maker=True)
                        cash -= cost + fee

                        if inventory >= 0:
                            avg_entry = (avg_entry * inventory + cost) / (inventory + fill_size)
                            inventory += fill_size
                        else:
                            # Closing short
                            pnl = (avg_entry - our_bid) * min(fill_size, abs(inventory))
                            total_pnl += pnl
                            winning_trips += 1 if pnl > 0 else 0
                            total_round_trips += 1
                            inventory += fill_size
                            avg_entry = our_bid if inventory > 0 else 0

                        session_fills += 1
                        total_fills += 1
                        max_inventory = max(max_inventory, abs(inventory))

                # Ask fill probability: 15% per tick if we're at or inside the ask
                elif our_ask <= snapshot.market_best_ask + 0.001 and random.random() < 0.15:
                    fill_size = config.quote_size
                    proceeds = fill_size * our_ask
                    fee = net_fill_fee(our_ask, fill_size, is_maker=True)
                    cash += proceeds - fee

                    if inventory > 0:
                        # Closing long
                        pnl = (our_ask - avg_entry) * min(fill_size, inventory)
                        total_pnl += pnl
                        winning_trips += 1 if pnl > 0 else 0
                        total_round_trips += 1
                        inventory -= fill_size
                        avg_entry = our_ask if inventory < 0 else 0
                    else:
                        inventory -= fill_size

                    session_fills += 1
                    total_fills += 1
                    max_inventory = max(max_inventory, abs(inventory))

            # Mark remaining inventory at expiry
            if inventory != 0:
                market_resolution = 1.0 if market_mid > 0.5 else 0.0
                liquidation_loss = abs(inventory) * abs(market_resolution - avg_entry)
                cash -= liquidation_loss
                total_pnl -= liquidation_loss

            equity = cash
            peak_equity = max(peak_equity, equity)
            max_drawdown = max(max_drawdown, peak_equity - equity)

        # Print results
        print("\n" + "=" * 70)
        print(f"REAL DATA BACKTEST RESULTS")
        print(f"Strategy: {config.min_yes_price:.2f}–{config.max_yes_price:.2f}")
        print("=" * 70)
        print(f"Total fills:        {total_fills}")
        print(f"Round trips:        {total_round_trips}")
        if total_round_trips > 0:
            print(f"Win rate:           {winning_trips / total_round_trips * 100:.1f}%")
            print(f"Avg P&L/trip:       ${total_pnl / total_round_trips:+.4f}")
        print(f"Total P&L:          ${total_pnl:+.2f}")
        print(f"Max inventory:      {max_inventory:.1f} shares")
        print(f"Max drawdown:       ${max_drawdown:.2f}")
        print("=" * 70 + "\n")


# ═══════════════════════════════════════════════════════════════════════════════
# RESOLVER BACKTEST: Replay historical data with directional resolver logic
# ═══════════════════════════════════════════════════════════════════════════════

def run_resolver_backtest():
    """
    Replay mm_historical.jsonl with directional resolver logic.

    Trades BOTH directions:
      - YES: when market_mid > ENTRY_MIN (e.g. 80¢) → buy YES at market ask
      - NO:  when market_mid < (1 - ENTRY_MIN) (e.g. 20¢) → buy NO at (1 - market_bid)

    Resolution inference: use last tick's YES price.
      - last_bid > 0.5 → YES won (BTC ended above strike)
      - last_bid < 0.5 → NO won  (BTC ended below strike)
      No sessions skipped — binary market always resolves one way.

    Entry gate:
      - seconds_to_expiry < RESOLVER_MAX_TTE (default 120s)
      - BTC 1m momentum must confirm direction
      - Only one trade per session
    """
    import json

    entry_min = float(os.getenv("RESOLVER_MIN_PRICE", "0.80"))
    entry_max = float(os.getenv("RESOLVER_MAX_PRICE", "0.95"))
    max_tte   = float(os.getenv("RESOLVER_MAX_TTE",   "120"))
    capital   = float(os.getenv("INITIAL_CAPITAL",    "100"))
    risk_pct  = 0.05   # 5% of capital per trade

    rows = []
    try:
        with open("data/mm_historical.jsonl") as f:
            for line in f:
                try: rows.append(json.loads(line))
                except json.JSONDecodeError: pass
    except FileNotFoundError:
        print("ERROR: data/mm_historical.jsonl not found"); return

    valid = [r for r in rows
             if r.get("market_best_bid", 0) > 0
             and r.get("market_best_ask", 1) < 1.0
             and r.get("seconds_to_expiry", 0) > 0]

    # Group into sessions (TTE resets when new market opens)
    sessions, cur, last_tte = [], [], valid[0].get("seconds_to_expiry", 300) if valid else 300
    for r in valid:
        tte = r.get("seconds_to_expiry", 300)
        if tte > last_tte + 10:
            if cur: sessions.append(cur)
            cur = []
        cur.append(r)
        last_tte = tte
    if cur: sessions.append(cur)

    print(f"\n{'='*65}")
    print(f"RESOLVER BACKTEST  |  capital=${capital:.0f}  risk={risk_pct*100:.0f}%/trade")
    print(f"Entry: {entry_min:.0%}–{entry_max:.0%} YES price  |  TTE < {max_tte:.0f}s")
    print(f"Sessions in data: {len(sessions)}  ({len(sessions)/6.6:.0f}/day over 6.6 days)")
    print(f"{'='*65}")

    cash = capital
    yes_trades = yes_wins = no_trades = no_wins = 0
    total_pnl = 0.0
    trade_log = []

    for session in sessions:
        # Resolution: last tick YES price > 0.5 → YES won
        last_bid  = session[-1].get("market_best_bid", 0.5)
        resolved_yes = last_bid > 0.5

        # Find first qualifying entry tick
        entry_row  = None
        trade_dir  = None   # "YES" or "NO"

        for row in session:
            bid  = row.get("market_best_bid", 0)
            ask  = row.get("market_best_ask", 1)
            mid  = (bid + ask) / 2
            tte  = row.get("seconds_to_expiry", 999)
            m1m  = row.get("btc_change_1m", 0.0)

            if tte > max_tte: continue

            # YES opportunity: market near 80-95¢, BTC 1m still rising (or flat)
            if entry_min <= mid <= entry_max and m1m >= 0:
                entry_row = row; trade_dir = "YES"; break

            # NO opportunity: market near 5-20¢ (NO is at 80-95¢), BTC 1m falling
            if (1 - entry_max) <= mid <= (1 - entry_min) and m1m <= 0:
                entry_row = row; trade_dir = "NO"; break

        if entry_row is None:
            continue

        # --- Simulate fill ---
        if trade_dir == "YES":
            fill_price = entry_row["market_best_ask"]          # buy YES at ask
            fee        = polymarket_taker_fee_amount(fill_price, 1)
            cost_per   = fill_price + fee
            size       = min(10.0, (cash * risk_pct) / cost_per)
            size       = max(0.1, round(size, 2))
            cost       = cost_per * size
            if cost > cash: continue
            cash      -= cost
            # Resolution
            won = resolved_yes
            gross = size * 1.0 if won else 0.0
            cash += gross
            pnl   = gross - cost
            yes_trades += 1
            yes_wins   += 1 if won else 0

        else:  # NO trade: buy NO = pay (1 - YES_bid), win if NO resolves
            no_price   = 1.0 - entry_row["market_best_bid"]    # NO ask ≈ 1 - YES_bid
            fee        = polymarket_taker_fee_amount(no_price, 1)
            cost_per   = no_price + fee
            size       = min(10.0, (cash * risk_pct) / cost_per)
            size       = max(0.1, round(size, 2))
            cost       = cost_per * size
            if cost > cash: continue
            cash      -= cost
            # Resolution
            won = not resolved_yes
            gross = size * 1.0 if won else 0.0
            cash += gross
            pnl   = gross - cost
            no_trades += 1
            no_wins   += 1 if won else 0

        total_pnl += pnl
        trade_log.append({
            "dir": trade_dir, "price": fill_price if trade_dir == "YES" else no_price,
            "size": size, "won": won, "pnl": pnl, "cash": cash,
        })

    # Print trade log
    for t in trade_log:
        print(f"  {t['dir']:3s}  entry={t['price']:.2f}  size={t['size']:.2f}  "
              f"{'WIN ' if t['won'] else 'LOSS'}  pnl=${t['pnl']:+.4f}  cash=${t['cash']:.2f}")

    # Summary
    total_trades = yes_trades + no_trades
    total_wins   = yes_wins   + no_wins
    print(f"\n{'='*65}")
    print(f"RESULTS  —  {len(sessions)} sessions  |  {len(sessions)/6.6:.0f} sessions/day")
    print(f"{'─'*65}")
    print(f"  YES trades:  {yes_trades:3d}   wins: {yes_wins:3d}   "
          f"({yes_wins/yes_trades*100:.0f}%)" if yes_trades else "  YES trades:    0")
    print(f"  NO  trades:  {no_trades:3d}   wins: {no_wins:3d}   "
          f"({no_wins/no_trades*100:.0f}%)" if no_trades else "  NO  trades:    0")
    print(f"  TOTAL:       {total_trades:3d}   wins: {total_wins:3d}   "
          f"({total_wins/total_trades*100:.1f}%)" if total_trades else "  TOTAL:         0")
    print(f"{'─'*65}")
    if total_trades:
        avg_pnl = total_pnl / total_trades
        trades_per_day = total_trades / 6.6
        print(f"  Avg P&L/trade:    ${avg_pnl:+.4f}")
        print(f"  Trades/day:       {trades_per_day:.1f}")
        print(f"  Est. P&L/day:     ${avg_pnl * trades_per_day:+.2f}")
    print(f"  Starting capital: ${capital:.2f}")
    print(f"  Final cash:       ${cash:.2f}")
    print(f"  Total P&L:        ${cash - capital:+.2f}  ({(cash/capital - 1)*100:+.1f}%)")
    entry_be = entry_min + polymarket_taker_fee_amount(entry_min, 1)
    print(f"\n  Break-even win rate at {entry_min:.0%} entry: {entry_be:.1%}")
    print(f"  (need true prob > {entry_be:.1%} to profit per trade)")
    print(f"{'='*65}\n")


# ═══════════════════════════════════════════════════════════════════════════════
# RESOLVER: Directional taker — buy near expiry at extreme prices
# ═══════════════════════════════════════════════════════════════════════════════

async def run_resolver():
    """
    Directional resolver bot.

    Waits for Polymarket YES price to enter extreme range (default 80–95¢)
    with < 90 seconds to expiry and BTC momentum confirming direction, then
    places a one-sided TAKER BUY and holds to resolution.

    Edge: captures market mispricing (~5¢) rather than spread (0.37¢).
    Gas ($0.005) is <1% of trade value vs 2,700% for market making at $15.

    Env config:
        RESOLVER_MIN_PRICE=0.80
        RESOLVER_MAX_PRICE=0.95
        RESOLVER_MAX_TTE=90
        PAPER_TRADING=true/false
    """
    if not HAS_HL_FEED:
        print("ERROR: hyperliquid_api.py not found."); return

    entry_min  = float(os.getenv("RESOLVER_MIN_PRICE", "0.80"))
    entry_max  = float(os.getenv("RESOLVER_MAX_PRICE", "0.95"))
    max_tte    = float(os.getenv("RESOLVER_MAX_TTE",   "120"))
    paper_mode = os.getenv("PAPER_TRADING", "true").lower() != "false"
    capital    = float(os.getenv("INITIAL_CAPITAL", "100"))

    hl_feed       = HyperliquidFeed(poll_interval=3.0)
    btc_feed      = BinanceBTCFeed()      if HAS_BINANCE_FEED   else None
    gamma_feed    = PolymarketGammaFeed() if HAS_GAMMA_FEED     else None
    chainlink_feed= ChainlinkBTCFeed()    if HAS_CHAINLINK_FEED else None
    confidence_calc = ConfidenceCalculator(max_inventory=15.0)
    data_loader   = HistoricalDataLoader()   # records every tick for backtest
    alerter       = AlertManager.from_env()

    manager = None
    if not paper_mode:
        if not HAS_LIVE_TRADER:
            print("ERROR: LiveOrderManager not available (install py-clob-client)."); return
        manager = LiveOrderManager()

    print(f"""
╔══════════════════════════════════════════════════════════════╗
║  DIRECTIONAL RESOLVER                                        ║
║  {'PAPER MODE — no real funds' if paper_mode else '⚠  LIVE MODE — REAL FUNDS ON POLYGON ⚠ '}{'                 ' if paper_mode else ''}║
║                                                              ║
║  Strategy: buy YES at {entry_min:.0%}–{entry_max:.0%} when TTE < {max_tte:.0f}s           ║
║  Capital:  ${capital:<10.2f}                                   ║
║  Press Ctrl+C to stop                                        ║
╚══════════════════════════════════════════════════════════════╝
    """)

    start_tasks = [hl_feed.start()]
    if btc_feed:      start_tasks.append(btc_feed.start())
    if gamma_feed:    start_tasks.append(gamma_feed.start())
    if chainlink_feed:start_tasks.append(chainlink_feed.start())
    if manager:       start_tasks.append(manager.start())
    await asyncio.gather(*start_tasks)
    await asyncio.sleep(10)  # let feeds connect

    # ── State ─────────────────────────────────────────────────────────────────
    cash          = capital
    _in_position  = False
    _trade_dir    = "YES"   # "YES" or "NO"
    _entry_price  = 0.0
    _entry_size   = 0.0
    _entry_fee    = 0.0
    _entry_market = ""
    _prev_tte     = 999.0
    total_trades  = 0
    wins = losses = 0
    total_pnl     = 0.0
    cycle         = 0

    logger.info(f"Resolver started  paper={paper_mode}  capital=${capital:.2f}")

    try:
        while True:
            hl_fields    = hl_feed.get_snapshot_fields()
            btc_fields   = btc_feed.get_snapshot_fields()   if btc_feed      else {}
            gamma_fields = gamma_feed.get_snapshot_fields() if gamma_feed     else {}
            cl_fields    = chainlink_feed.get_snapshot_fields() if chainlink_feed else {}

            if chainlink_feed and btc_feed and btc_feed.price:
                chainlink_feed.binance_price = btc_feed.price

            snapshot = SideDataSnapshot(
                btc_price        = btc_fields.get("btc_price") or hl_fields["hl_oracle_price"],
                btc_change_1m    = btc_fields.get("btc_change_1m", 0.0),
                btc_change_5m    = btc_fields.get("btc_change_5m", 0.0),
                btc_volatility_1m= btc_fields.get("btc_volatility_1m", 0.001),
                hl_oracle_price  = hl_fields["hl_oracle_price"],
                hl_funding_rate  = hl_fields["hl_funding_rate"],
                hl_open_interest = hl_fields["hl_open_interest"],
                cvd_signal       = hl_fields["cvd_signal"],
                liq_signal       = hl_fields["liq_signal"],
                funding_signal   = hl_fields["funding_signal"],
                oi_signal        = hl_fields["oi_signal"],
                chainlink_price  = cl_fields.get("chainlink_price", 0.0),
                market_spread    = gamma_fields.get("market_spread", 0.0),
                market_best_bid  = gamma_feed.best_bid  if gamma_feed else 0.0,
                market_best_ask  = gamma_feed.best_ask  if gamma_feed else 1.0,
                seconds_to_expiry= gamma_fields.get("seconds_to_expiry", 300.0),
                timestamp        = time.time(),
            )

            # Record every tick — this is how we build the historical dataset
            data_loader.record_tick(snapshot)

            feed_timestamps = {
                "binance":     time.time() if (btc_feed and btc_feed.is_connected) else 0.0,
                "hyperliquid": time.time() if hl_feed.is_connected else 0.0,
                "gamma":       time.time() if (gamma_feed and gamma_feed.is_fresh) else 0.0,
                "chainlink":   (chainlink_feed._last_update if chainlink_feed else 0.0),
            }

            confidence = confidence_calc.score(
                snapshot,
                net_inventory  = 1.0 if _in_position else 0.0,
                feed_timestamps= feed_timestamps,
                consecutive_losses = losses,
            )

            market_bid = snapshot.market_best_bid
            market_ask = snapshot.market_best_ask
            market_mid = (market_bid + market_ask) / 2 if market_bid > 0 else 0.0
            tte        = snapshot.seconds_to_expiry

            # ── Detect market expiry (TTE reset = new window) ─────────────────
            if _in_position and tte > _prev_tte + 10:
                # Market rolled over — infer resolution from last mid price
                # YES wins if last mid > 0.5, NO wins if last mid < 0.5
                yes_won = market_mid > 0.5 if market_mid > 0 else True
                if _trade_dir == "YES":
                    resolution_price = 1.0 if yes_won else 0.0
                else:
                    resolution_price = 1.0 if not yes_won else 0.0
                gross   = resolution_price * _entry_size
                pnl     = gross - (_entry_price * _entry_size + _entry_fee)
                cash   += gross
                total_pnl += pnl
                total_trades += 1
                wins   += 1 if pnl > 0 else 0
                losses += 1 if pnl <= 0 else 0
                _in_position = False
                logger.info(
                    f"RESOLVER EXIT (expiry rollover)  resolve@{resolution_price:.2f}  "
                    f"pnl=${pnl:+.4f}  cash=${cash:.2f}"
                )
                if alerter.enabled:
                    await alerter.send(
                        f"{'✅' if pnl>0 else '❌'} <b>Resolver closed</b>  "
                        f"Pnl: ${pnl:+.4f}  Cash: ${cash:.2f}"
                    )

            _prev_tte = tte

            # ── Entry gate ────────────────────────────────────────────────────
            if not _in_position and market_mid > 0:
                tte_ok    = tte <= max_tte
                conf_ok   = confidence.tier in ("FULL", "REDUCED")
                warmed_up = abs(snapshot.btc_change_5m) > 0  # feeds ready

                # Check BOTH directions
                yes_signal = (entry_min <= market_mid <= entry_max
                              and snapshot.btc_change_1m >= 0)   # BTC rising → YES
                no_signal  = ((1 - entry_max) <= market_mid <= (1 - entry_min)
                              and snapshot.btc_change_1m <= 0)   # BTC falling → NO

                in_range = yes_signal or no_signal
                # Which direction wins?
                trade_direction = "YES" if yes_signal else "NO"

                if cycle % 10 == 0:
                    no_mid = round(1.0 - market_mid, 3) if market_mid > 0 else 0.0
                    waiting_for = []
                    if not tte_ok:
                        waiting_for.append(f"TTE<{max_tte:.0f}s (now {tte:.0f}s)")
                    if not in_range:
                        waiting_for.append(f"price 80-95¢ (now YES={market_mid:.0%} NO={no_mid:.0%})")
                    if not conf_ok:
                        waiting_for.append(f"confidence (now {confidence.tier})")
                    status = "WAITING for: " + ", ".join(waiting_for) if waiting_for else "READY TO TRADE"
                    logger.info(
                        f"[RESOLVER]  YES={market_mid*100:.1f}¢  NO={no_mid*100:.1f}¢  "
                        f"TTE={tte:.0f}s  BTC1m={snapshot.btc_change_1m*100:+.3f}%  "
                        f"conf={confidence.tier}  |  {status}"
                    )

                if warmed_up and in_range and tte_ok and conf_ok:
                    # ── SIZE ──────────────────────────────────────────────────
                    if trade_direction == "YES":
                        fill_price = market_ask                       # buy YES at ask
                        token_id   = gamma_feed.yes_token_id if gamma_feed else None
                    else:
                        fill_price = round(1.0 - market_bid, 4)      # buy NO ≈ 1 - YES_bid
                        token_id   = gamma_feed.no_token_id  if gamma_feed else None

                    position_size = min(10.0, cash * 0.05 / fill_price)
                    position_size = max(0.1, round(position_size, 2))
                    fee           = polymarket_taker_fee_amount(fill_price, position_size)
                    cost          = fill_price * position_size + fee

                    if cost <= cash:
                        if paper_mode:
                            cash -= cost
                            _in_position  = True
                            _trade_dir    = trade_direction
                            _entry_price  = fill_price
                            _entry_size   = position_size
                            _entry_fee    = fee
                            _entry_market = (gamma_feed._condition_id or "") if gamma_feed else ""
                            logger.info(
                                f"RESOLVER ENTRY (paper)  buy {trade_direction} @ {fill_price:.4f}  "
                                f"size={position_size:.2f}  fee=${fee:.4f}  "
                                f"tte={tte:.0f}s  mid={market_mid:.3f}  "
                                f"btc1m={snapshot.btc_change_1m*100:+.3f}%"
                            )
                        else:
                            # Live: taker BUY via CLOB
                            mid_id = (gamma_feed._condition_id or "") if gamma_feed else ""
                            if token_id:
                                order_id = await manager._post_order(
                                    token_id, fill_price, position_size, "BUY", mid_id
                                )
                                if order_id:
                                    cash -= cost
                                    _in_position  = True
                                    _trade_dir    = trade_direction
                                    _entry_price  = fill_price
                                    _entry_size   = position_size
                                    _entry_fee    = fee
                                    _entry_market = mid_id
                                    logger.info(
                                        f"RESOLVER ENTRY (live)  buy {trade_direction} @ {fill_price:.4f}  "
                                        f"size={position_size:.2f}  fee=${fee:.4f}  "
                                        f"order_id={order_id}"
                                    )
                        if _in_position and alerter.enabled:
                            await alerter.send(
                                f"🎯 <b>Resolver entry</b>  "
                                f"{'Paper' if paper_mode else 'LIVE'}\n"
                                f"Buy {trade_direction} @ {fill_price*100:.1f}¢  size={position_size:.2f}sh\n"
                                f"TTE: {tte:.0f}s  BTC 1m: {snapshot.btc_change_1m*100:+.3f}%\n"
                                f"Cash after: ${cash:.2f}"
                            )

            # ── Status log every 60 cycles ────────────────────────────────────
            if cycle % 60 == 0:
                no_price = round(1.0 - market_mid, 3) if market_mid > 0 else 0.0
                pos_str = f"IN {_trade_dir} @ {_entry_price:.2f}" if _in_position else "flat"
                logger.info(
                    f"[STATUS]  cash=${cash:.2f}  pnl=${total_pnl:+.4f}  trades={total_trades}  "
                    f"W/L={wins}/{losses}  position={pos_str}  |  "
                    f"YES={market_mid*100:.1f}¢  NO={no_price*100:.1f}¢  TTE={tte:.0f}s"
                )

            cycle += 1
            await asyncio.sleep(1)

    except KeyboardInterrupt:
        logger.info("Resolver stopped by user")
    finally:
        if manager:
            await manager.stop()
        stop_tasks = [hl_feed.stop()]
        if btc_feed:       stop_tasks.append(btc_feed.stop())
        if gamma_feed:     stop_tasks.append(gamma_feed.stop())
        if chainlink_feed: stop_tasks.append(chainlink_feed.stop())
        await asyncio.gather(*stop_tasks)

        print(f"\n{'='*50}")
        print(f"RESOLVER SESSION SUMMARY")
        print(f"  Trades:    {total_trades}  (W:{wins} / L:{losses})")
        if total_trades:
            print(f"  Win rate:  {wins/total_trades*100:.1f}%")
            print(f"  Avg P&L:   ${total_pnl/total_trades:+.4f}/trade")
        print(f"  Total P&L: ${total_pnl:+.4f}")
        print(f"  Final cash: ${cash:.2f}  (started ${capital:.2f})")
        print(f"{'='*50}\n")


def main():
    parser = argparse.ArgumentParser(description="Enhanced Market Maker + Backtester")
    parser.add_argument("--backtest", action="store_true",
                        help="Run parameter sweep backtester (synthetic data)")
    parser.add_argument("--realtest", action="store_true",
                        help="Run real data backtest using mm_historical.jsonl")
    parser.add_argument("--resolver-test", action="store_true",
                        help="Backtest directional resolver on real historical data")
    parser.add_argument("--resolver", action="store_true",
                        help="Directional resolver: buy YES near expiry at extreme prices")
    parser.add_argument("--signals", action="store_true",
                        help="Run live signal monitor (connects to Hyperliquid)")
    parser.add_argument("--paper", action="store_true",
                        help="Run autonomous paper trader with confidence scoring")
    parser.add_argument("--live", action="store_true",
                        help="Live trading via Polymarket CLOB (real funds on Polygon)")
    parser.add_argument("--dual", action="store_true",
                        help="Live trading with SharedPositionLock (MM + directional co-exist)")
    args = parser.parse_args()

    if args.backtest:
        from backtests.backtest_unified import run_unified_backtest
        run_unified_backtest()
    elif getattr(args, "resolver_test", False):
        run_resolver_backtest()
    elif args.resolver:
        asyncio.run(run_resolver())
    elif args.realtest:
        # Run three real data backtest configurations
        backtester = RealDataBacktester()

        print("\n" + "="*70)
        print("CONFIG A: All prices (0.0–1.0) — unrestricted")
        print("="*70)
        backtester.run(RealBacktestConfig(min_yes_price=0.0, max_yes_price=1.0))

        print("\n" + "="*70)
        print("CONFIG B: High-prob markets (0.80–0.95) — extreme prices only")
        print("="*70)
        backtester.run(RealBacktestConfig(min_yes_price=0.80, max_yes_price=0.95))

        print("\n" + "="*70)
        print("CONFIG C: High-prob + near-expiry (<2min, 0.80–0.95)")
        print("="*70)
        backtester.run(RealBacktestConfig(min_yes_price=0.80, max_yes_price=0.95, max_tte=120.0))
    elif args.signals:
        asyncio.run(run_signal_monitor())
    elif args.paper:
        asyncio.run(run_paper_trader())
    elif args.live:
        asyncio.run(run_live_trader(dual=False))
    elif args.dual:
        asyncio.run(run_live_trader(dual=True))
    else:
        print("""
╔══════════════════════════════════════════════════════════════╗
║  ENHANCED MARKET MAKER                                       ║
╠══════════════════════════════════════════════════════════════╣
║                                                              ║
║  Available modes:                                            ║
║                                                              ║
║    --resolver        Directional resolver (paper/live)       ║
║    --resolver-test   Backtest resolver on real data          ║
║    --paper           Market maker paper trader               ║
║    --live            Market maker live (REAL FUNDS)          ║
║    --dual            Live + SharedPositionLock               ║
║    --signals         Signal monitor only                     ║
║    --backtest        Synthetic parameter sweep               ║
║                                                              ║
║  Dashboard (run in separate terminal):                       ║
║    python mm_dashboard.py            Terminal UI (rich)      ║
║    python mm_dashboard.py --web      Browser UI (port 8889)  ║
║                                                              ║
╚══════════════════════════════════════════════════════════════╝
        """)


if __name__ == "__main__":
    main()
