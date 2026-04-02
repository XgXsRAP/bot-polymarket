"""
╔══════════════════════════════════════════════════════════════╗
║  IMPROVED MARKET MAKER BACKTESTER                            ║
║                                                              ║
║  Key improvements over mm_enhanced_1.py backtester:          ║
║    1. Uses REAL Binance 1m candles (not synthetic paths)     ║
║    2. Models fills with spread + volume heuristics           ║
║    3. Adverse selection scales with volatility               ║
║    4. Walk-forward validation (detect overfitting)           ║
║    5. Monte Carlo resampling for confidence intervals        ║
║                                                              ║
║  Usage:                                                      ║
║    python improved_backtest.py                               ║
║    python improved_backtest.py --days 14                     ║
║    python improved_backtest.py --optimize                    ║
║    python improved_backtest.py --walk-forward --splits 5     ║
║    python improved_backtest.py --monte-carlo --runs 500      ║
╚══════════════════════════════════════════════════════════════╝
"""

import json
import math
import time
import random
import argparse
import statistics
from collections import deque
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional
from itertools import product as cart_product

try:
    import requests
except ImportError:
    print("❌ Missing: pip install requests")
    raise SystemExit(1)


# ═══════════════════════════════════════════════════════════
#  CONFIGURATION
# ═══════════════════════════════════════════════════════════

@dataclass
class MMBacktestConfig:
    """
    Market maker parameters to test.
    Each field maps to a quoting decision.
    """
    # Spread parameters
    base_spread: float = 0.04           # 4 cents baseline
    max_spread: float = 0.12            # Maximum spread in high vol
    min_spread: float = 0.015           # Floor — never go tighter
    volatility_multiplier: float = 5.0  # spread += vol * this

    # Size parameters
    quote_size: float = 20.0            # Shares per side
    min_quote_size: float = 5.0         # Floor
    max_inventory: float = 100.0        # Hard limit

    # Inventory management
    skew_factor: float = 0.002          # Quote skew per unit inventory
    inventory_fade_rate: float = 0.001  # Passive inventory reduction per tick

    # Signal weights (0.0 = off, 1.0 = full)
    use_momentum: bool = True
    momentum_spread_weight: float = 0.3   # How much momentum widens spread
    momentum_skew_weight: float = 0.5     # How much momentum shifts fair value

    # Volatility adaptation
    vol_window_seconds: int = 60          # Window for vol calculation
    vol_regime_threshold: float = 0.003   # Above this = "high vol"

    # Expiry behavior
    expiry_cutoff_seconds: float = 30.0   # Stop quoting this close to expiry
    expiry_spread_multiplier: float = 2.0 # Widen spread near expiry

    # Simulation settings
    market_duration_seconds: int = 300    # 5-minute markets
    tick_interval_seconds: float = 1.0    # 1 tick per second

    # Cost model
    taker_fee_pct: float = 0.002          # 0.2% Polymarket taker fee
    gas_cost_per_trade: float = 0.001     # ~$0.001 on Polygon


# ═══════════════════════════════════════════════════════════
#  DATA LOADER — Real Binance 1m Candles
# ═══════════════════════════════════════════════════════════

def fetch_binance_candles(days: int = 7, symbol: str = "BTCUSDT") -> list[dict]:
    """
    Fetch real 1-minute BTC candles from Binance.
    Returns list of {open, high, low, close, volume, timestamp}.
    """
    candles = []
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - (days * 86400 * 1000)

    print(f"📡 Fetching {days} days of {symbol} 1m candles from Binance...")

    current = start_ms
    while current < end_ms:
        try:
            params = {
                "symbol": symbol,
                "interval": "1m",
                "startTime": current,
                "limit": 1000,
            }
            r = requests.get(
                "https://api.binance.com/api/v3/klines",
                params=params, timeout=10,
            )
            r.raise_for_status()
            data = r.json()

            if not data:
                break

            for k in data:
                candles.append({
                    "timestamp": k[0] / 1000.0,
                    "open": float(k[1]),
                    "high": float(k[2]),
                    "low": float(k[3]),
                    "close": float(k[4]),
                    "volume": float(k[5]),
                })

            current = int(data[-1][0]) + 60000  # Next minute
            time.sleep(0.15)  # Rate limit respect

        except Exception as e:
            print(f"  ⚠️ Fetch error at {datetime.fromtimestamp(current/1000)}: {e}")
            current += 60000 * 100  # Skip ahead
            time.sleep(1)

    print(f"  ✅ Loaded {len(candles)} candles ({len(candles)/1440:.1f} days)")
    return candles


def load_cached_candles(filepath: str = "data/btc_candles.json", days: int = 7) -> list[dict]:
    """Load from cache or fetch fresh."""
    path = Path(filepath)
    if path.exists():
        age_hours = (time.time() - path.stat().st_mtime) / 3600
        if age_hours < 24:
            with open(filepath) as f:
                candles = json.load(f)
            print(f"📂 Loaded {len(candles)} cached candles ({age_hours:.1f}h old)")
            return candles

    candles = fetch_binance_candles(days=days)
    Path("data").mkdir(exist_ok=True)
    with open(filepath, "w") as f:
        json.dump(candles, f)
    return candles


# ═══════════════════════════════════════════════════════════
#  MARKET SIMULATOR — Generates Polymarket-like contract prices
# ═══════════════════════════════════════════════════════════

@dataclass
class SimulatedMarket:
    """One 5-minute BTC prediction market."""
    open_price: float          # BTC price at market open
    strike_price: float        # The price BTC needs to beat
    start_time: float          # Unix timestamp
    duration: float = 300.0    # 5 minutes

    @property
    def end_time(self) -> float:
        return self.start_time + self.duration


def btc_to_contract_price(btc_price: float, strike: float, seconds_left: float,
                           btc_vol_per_second: float) -> float:
    """
    Convert BTC price to YES contract probability using a simplified
    Black-Scholes-like model.

    This is more realistic than the linear mapping in the original backtester.
    As time runs out, prices converge to 0 or 1 (binary expiry).
    """
    if seconds_left <= 0:
        return 1.0 if btc_price > strike else 0.0

    # Distance from strike in standard deviations
    if btc_vol_per_second <= 0:
        btc_vol_per_second = 0.0001

    z = (btc_price - strike) / (strike * btc_vol_per_second * math.sqrt(seconds_left))

    # Approximate normal CDF with logistic function (fast, close enough)
    prob = 1.0 / (1.0 + math.exp(-1.7 * z))

    # Clamp to realistic contract price range
    return max(0.02, min(0.98, prob))


# ═══════════════════════════════════════════════════════════
#  FILL SIMULATOR — Models whether quotes get filled
# ═══════════════════════════════════════════════════════════

class FillSimulator:
    """
    Models fill probability based on:
    - Price distance from mid (tighter quotes fill more)
    - Volume (higher volume = more fills)
    - Volatility (higher vol = more fills but more adverse)
    - Time to expiry (more fills near expiry as traders rush)
    """

    def __init__(self, base_fill_rate: float = 0.08):
        self.base_fill_rate = base_fill_rate

    def fill_probability(self, spread: float, volume_ratio: float,
                          volatility: float, seconds_left: float) -> float:
        """
        Probability that a quote at this spread gets filled this tick.

        spread: current bid-ask spread (e.g., 0.04)
        volume_ratio: current volume / average volume
        volatility: recent price vol (std of 1m returns)
        seconds_left: time to market expiry
        """
        # Tighter spread → more fills (inverse relationship)
        spread_factor = max(0.2, 0.04 / max(spread, 0.01))

        # Higher volume → more fills
        vol_factor = min(2.0, max(0.3, volume_ratio))

        # More volatile → more fills (but also more adverse)
        volatility_factor = min(2.0, max(0.5, 1.0 + volatility * 100))

        # Near expiry → slightly more fills (panic trading)
        expiry_factor = 1.0
        if seconds_left < 60:
            expiry_factor = 1.0 + (60 - seconds_left) / 60 * 0.5

        prob = self.base_fill_rate * spread_factor * vol_factor * volatility_factor * expiry_factor
        return min(0.4, prob)  # Cap at 40% per tick

    def is_adverse(self, volatility: float, momentum_abs: float) -> bool:
        """
        Determine if a fill is adverse (informed trader picking us off).

        Higher volatility = higher adverse selection.
        Strong momentum = higher adverse selection.
        """
        # Base adverse rate: 20%
        base = 0.20

        # Vol adjustment: up to +30% in high vol
        vol_adj = min(0.30, volatility * 50)

        # Momentum adjustment: up to +20% in strong trends
        mom_adj = min(0.20, momentum_abs * 20)

        adverse_prob = base + vol_adj + mom_adj
        return random.random() < adverse_prob


# ═══════════════════════════════════════════════════════════
#  QUOTING ENGINE — Generates bid/ask prices
# ═══════════════════════════════════════════════════════════

class QuotingEngine:
    """Generates quotes based on fair value and market conditions."""

    def __init__(self, cfg: MMBacktestConfig):
        self.cfg = cfg

    def generate_quotes(self, fair_value: float, net_inventory: float,
                         volatility: float, momentum_1m: float,
                         seconds_left: float) -> dict:
        """
        Generate bid/ask quotes for YES and NO sides.

        Returns dict with: yes_bid, yes_ask, no_bid, no_ask, spread, size
        """
        cfg = self.cfg

        # ── Spread calculation ──
        spread = cfg.base_spread

        # Widen for volatility
        vol_spread = volatility * cfg.volatility_multiplier * 100  # scale to cents
        spread += vol_spread

        # Widen for momentum (trending = more adverse selection risk)
        if cfg.use_momentum:
            mom_spread = abs(momentum_1m) * cfg.momentum_spread_weight
            spread += mom_spread

        # Widen near expiry
        if seconds_left < 60:
            expiry_mult = 1.0 + (60 - seconds_left) / 60 * (cfg.expiry_spread_multiplier - 1)
            spread *= expiry_mult

        # Clamp spread
        spread = max(cfg.min_spread, min(cfg.max_spread, spread))
        half_spread = spread / 2

        # ── Inventory skew ──
        skew = net_inventory * cfg.skew_factor

        # ── Momentum skew (shift fair value in momentum direction) ──
        if cfg.use_momentum:
            mom_skew = momentum_1m * cfg.momentum_skew_weight * 10  # scale
            skew += max(-0.03, min(0.03, mom_skew))

        # ── Generate prices ──
        yes_bid = max(0.01, fair_value - half_spread - skew)
        yes_ask = min(0.99, fair_value + half_spread - skew)
        if yes_bid >= yes_ask:
            yes_bid = yes_ask - 0.01

        no_fair = 1.0 - fair_value
        no_bid = max(0.01, no_fair - half_spread + skew)
        no_ask = min(0.99, no_fair + half_spread + skew)
        if no_bid >= no_ask:
            no_bid = no_ask - 0.01

        # ── Size adjustment ──
        size = cfg.quote_size
        inv_pct = abs(net_inventory) / cfg.max_inventory
        size *= max(0.25, 1 - inv_pct)
        size = max(cfg.min_quote_size, size)

        return {
            "yes_bid": round(yes_bid, 4),
            "yes_ask": round(yes_ask, 4),
            "no_bid": round(no_bid, 4),
            "no_ask": round(no_ask, 4),
            "spread": round(spread, 4),
            "size": round(size, 1),
            "skew": round(skew, 4),
        }


# ═══════════════════════════════════════════════════════════
#  BACKTEST RESULT
# ═══════════════════════════════════════════════════════════

@dataclass
class MMBacktestResult:
    config_name: str = ""
    config: dict = field(default_factory=dict)
    total_markets: int = 0
    total_fills: int = 0
    round_trips: int = 0
    adverse_fills: int = 0
    spread_pnl: float = 0.0
    inventory_pnl: float = 0.0
    total_pnl: float = 0.0
    max_inventory: float = 0.0
    avg_spread: float = 0.0
    avg_fill_rate: float = 0.0
    pnl_per_fill: float = 0.0
    pnl_per_market: float = 0.0
    sharpe_ratio: float = 0.0
    max_drawdown: float = 0.0
    calmar_ratio: float = 0.0  # annual return / max drawdown
    win_rate: float = 0.0      # % of round trips profitable


# ═══════════════════════════════════════════════════════════
#  MAIN BACKTESTER
# ═══════════════════════════════════════════════════════════

class ImprovedMMBacktester:
    """
    Backtests market making strategy against real BTC price data.

    For each 5-minute window in the candle data:
    1. Creates a simulated Polymarket contract
    2. Computes fair value from BTC price
    3. Generates bid/ask quotes
    4. Simulates fills based on price movement and volume
    5. Tracks inventory, P&L, and risk metrics
    """

    def __init__(self, candles: list[dict]):
        self.candles = candles
        self.fill_sim = FillSimulator()

    def run(self, cfg: MMBacktestConfig, config_name: str = "",
            verbose: bool = False) -> MMBacktestResult:
        """Run a single backtest with the given configuration."""

        quoting = QuotingEngine(cfg)

        # State tracking
        net_inventory = 0.0
        total_fills = 0
        adverse_fills = 0
        round_trips = 0
        spread_pnl = 0.0
        running_pnl = 0.0
        max_inv = 0.0
        peak_pnl = 0.0
        max_drawdown = 0.0
        spreads = []
        market_pnls = []
        buy_queue = []  # Queue of buy prices for round-trip matching

        # Rolling volatility computation
        price_history = deque(maxlen=max(60, cfg.vol_window_seconds))
        volume_history = deque(maxlen=300)

        # Group candles into 5-minute market windows
        if not self.candles:
            return MMBacktestResult(config_name=config_name)

        market_size = cfg.market_duration_seconds // 60  # candles per market
        num_markets = len(self.candles) // market_size

        for market_idx in range(num_markets):
            start_idx = market_idx * market_size
            end_idx = start_idx + market_size
            market_candles = self.candles[start_idx:end_idx]

            if len(market_candles) < market_size:
                break

            # Market parameters
            open_price = market_candles[0]["open"]
            strike = open_price  # "Will BTC be higher than open?"
            market_start_pnl = running_pnl

            # Per-market inventory tracking
            market_inventory = 0.0

            # Simulate each second within the market (interpolate between 1m candles)
            for candle_idx, candle in enumerate(market_candles):
                # Simulate multiple ticks within each 1-minute candle
                ticks_per_candle = 60
                price_range = candle["high"] - candle["low"]

                for tick in range(ticks_per_candle):
                    # Interpolate price within the candle
                    t_frac = tick / ticks_per_candle
                    # Simulate a random walk between OHLC
                    if tick == 0:
                        btc_price = candle["open"]
                    elif tick == ticks_per_candle - 1:
                        btc_price = candle["close"]
                    else:
                        btc_price = candle["low"] + random.random() * price_range

                    # Time within market
                    elapsed = candle_idx * 60 + tick
                    seconds_left = cfg.market_duration_seconds - elapsed

                    if seconds_left <= cfg.expiry_cutoff_seconds:
                        break  # Stop quoting near expiry

                    # Update rolling stats
                    price_history.append(btc_price)
                    volume_history.append(candle["volume"] / 60)  # Per-second volume

                    # Compute volatility (std of returns over window)
                    if len(price_history) >= 10:
                        returns = [
                            (price_history[i] - price_history[i-1]) / price_history[i-1]
                            for i in range(1, len(price_history))
                        ]
                        volatility = statistics.stdev(returns) if len(returns) > 1 else 0.001
                    else:
                        volatility = 0.001

                    # Compute momentum
                    momentum_1m = 0.0
                    if len(price_history) >= 60:
                        momentum_1m = (btc_price - price_history[-60]) / price_history[-60]
                    elif len(price_history) >= 10:
                        momentum_1m = (btc_price - price_history[0]) / price_history[0]

                    # Volume ratio
                    avg_vol = sum(volume_history) / len(volume_history) if volume_history else 1.0
                    vol_ratio = (candle["volume"] / 60) / max(avg_vol, 0.01)

                    # Fair value of YES contract
                    fair_value = btc_to_contract_price(
                        btc_price, strike, seconds_left,
                        max(volatility, 0.0001)
                    )

                    # Generate quotes
                    quotes = quoting.generate_quotes(
                        fair_value, net_inventory, volatility,
                        momentum_1m, seconds_left
                    )
                    spreads.append(quotes["spread"])

                    # Inventory limit check
                    if abs(net_inventory) >= cfg.max_inventory:
                        continue  # Skip quoting when at limit

                    # ── Simulate fills ──
                    fill_prob = self.fill_sim.fill_probability(
                        quotes["spread"], vol_ratio, volatility, seconds_left
                    )

                    # Bid fill (someone sells to us → we buy)
                    if random.random() < fill_prob:
                        total_fills += 1
                        buy_price = quotes["yes_bid"]
                        net_inventory += quotes["size"]
                        market_inventory += quotes["size"]
                        max_inv = max(max_inv, abs(net_inventory))
                        buy_queue.append(buy_price)

                        # Check if adverse
                        if self.fill_sim.is_adverse(volatility, abs(momentum_1m)):
                            adverse_fills += 1
                            # Adverse fill: price will move against us
                            adverse_cost = quotes["size"] * quotes["spread"] * 0.3
                            running_pnl -= adverse_cost

                        # Cost
                        running_pnl -= quotes["size"] * buy_price * cfg.taker_fee_pct
                        running_pnl -= cfg.gas_cost_per_trade

                    # Ask fill (someone buys from us → we sell)
                    if random.random() < fill_prob and buy_queue:
                        total_fills += 1
                        sell_price = quotes["yes_ask"]
                        net_inventory -= quotes["size"]
                        market_inventory -= quotes["size"]

                        # Round trip P&L
                        buy_price = buy_queue.pop(0)
                        trip_pnl = (sell_price - buy_price) * quotes["size"]
                        spread_pnl += trip_pnl
                        running_pnl += trip_pnl
                        round_trips += 1

                        # Cost
                        running_pnl -= quotes["size"] * sell_price * cfg.taker_fee_pct
                        running_pnl -= cfg.gas_cost_per_trade

                    # Track drawdown
                    peak_pnl = max(peak_pnl, running_pnl)
                    drawdown = peak_pnl - running_pnl
                    max_drawdown = max(max_drawdown, drawdown)

            # End of market: close remaining inventory at mid
            if abs(market_inventory) > 0:
                close_penalty = abs(market_inventory) * cfg.base_spread * 0.5
                running_pnl -= close_penalty

            market_pnls.append(running_pnl - market_start_pnl)

        # ── Compute summary statistics ──
        if market_pnls and len(market_pnls) > 1:
            avg_return = sum(market_pnls) / len(market_pnls)
            std_return = statistics.stdev(market_pnls) if len(market_pnls) > 1 else 1.0
            sharpe = (avg_return / std_return) * math.sqrt(252 * 288) if std_return > 0 else 0
            # 288 = number of 5-min markets per day
        else:
            sharpe = 0.0

        profitable_trips = sum(1 for p in market_pnls if p > 0)
        win_rate = profitable_trips / len(market_pnls) if market_pnls else 0

        calmar = 0.0
        if max_drawdown > 0 and running_pnl > 0:
            annual_return = running_pnl * (365 * 288 / max(len(market_pnls), 1))
            calmar = annual_return / max_drawdown

        return MMBacktestResult(
            config_name=config_name,
            config=asdict(cfg),
            total_markets=num_markets,
            total_fills=total_fills,
            round_trips=round_trips,
            adverse_fills=adverse_fills,
            spread_pnl=round(spread_pnl, 4),
            inventory_pnl=round(running_pnl - spread_pnl, 4),
            total_pnl=round(running_pnl, 4),
            max_inventory=round(max_inv, 1),
            avg_spread=round(sum(spreads)/len(spreads), 4) if spreads else 0,
            avg_fill_rate=round(total_fills / max(num_markets, 1), 2),
            pnl_per_fill=round(running_pnl / max(total_fills, 1), 4),
            pnl_per_market=round(running_pnl / max(num_markets, 1), 4),
            sharpe_ratio=round(sharpe, 2),
            max_drawdown=round(max_drawdown, 4),
            calmar_ratio=round(calmar, 2),
            win_rate=round(win_rate, 4),
        )

    def run_parameter_sweep(self, verbose: bool = True) -> list[MMBacktestResult]:
        """Test systematic parameter variations."""
        results = []

        # ── Test 1: Spread Width ──
        if verbose:
            print("\n📊 TEST 1: Base Spread Width")
            print("=" * 80)

        for spread in [0.02, 0.03, 0.04, 0.05, 0.06, 0.08]:
            cfg = MMBacktestConfig(base_spread=spread)
            r = self.run(cfg, f"spread_{spread:.2f}")
            results.append(r)
            if verbose:
                print(f"  Spread {spread:.2f} | P&L: ${r.total_pnl:+8.2f} | "
                      f"Fills: {r.total_fills:5d} | Sharpe: {r.sharpe_ratio:+6.2f} | "
                      f"MaxDD: ${r.max_drawdown:.2f} | WR: {r.win_rate*100:.1f}%")

        # ── Test 2: Volatility Multiplier ──
        if verbose:
            print(f"\n📊 TEST 2: Volatility Spread Multiplier")
            print("=" * 80)

        for vm in [0.0, 2.0, 5.0, 8.0, 12.0]:
            cfg = MMBacktestConfig(volatility_multiplier=vm)
            r = self.run(cfg, f"volmult_{vm:.0f}")
            results.append(r)
            if verbose:
                print(f"  VolMult {vm:4.0f} | P&L: ${r.total_pnl:+8.2f} | "
                      f"Fills: {r.total_fills:5d} | Sharpe: {r.sharpe_ratio:+6.2f} | "
                      f"AvgSpread: {r.avg_spread:.3f}")

        # ── Test 3: Inventory Skew ──
        if verbose:
            print(f"\n📊 TEST 3: Inventory Skew Factor")
            print("=" * 80)

        for skew in [0.0, 0.001, 0.002, 0.004, 0.008]:
            cfg = MMBacktestConfig(skew_factor=skew)
            r = self.run(cfg, f"skew_{skew:.3f}")
            results.append(r)
            if verbose:
                print(f"  Skew {skew:.3f} | P&L: ${r.total_pnl:+8.2f} | "
                      f"MaxInv: {r.max_inventory:5.0f} | MaxDD: ${r.max_drawdown:.2f} | "
                      f"Sharpe: {r.sharpe_ratio:+6.2f}")

        # ── Test 4: Momentum Signals On vs Off ──
        if verbose:
            print(f"\n📊 TEST 4: Momentum Signals Impact")
            print("=" * 80)

        for use_mom, label in [(False, "no_momentum"), (True, "with_momentum")]:
            cfg = MMBacktestConfig(use_momentum=use_mom)
            r = self.run(cfg, label)
            results.append(r)
            if verbose:
                mom_str = "ON " if use_mom else "OFF"
                print(f"  Momentum {mom_str} | P&L: ${r.total_pnl:+8.2f} | "
                      f"Sharpe: {r.sharpe_ratio:+6.2f} | "
                      f"Adverse: {r.adverse_fills}/{r.total_fills}")

        # ── Test 5: Quote Size ──
        if verbose:
            print(f"\n📊 TEST 5: Quote Size")
            print("=" * 80)

        for size in [5, 10, 15, 20, 30, 50]:
            cfg = MMBacktestConfig(quote_size=float(size))
            r = self.run(cfg, f"size_{size}")
            results.append(r)
            if verbose:
                print(f"  Size {size:3d} | P&L: ${r.total_pnl:+8.2f} | "
                      f"MaxInv: {r.max_inventory:5.0f} | MaxDD: ${r.max_drawdown:.2f}")

        return results

    def walk_forward(self, cfg: MMBacktestConfig, n_splits: int = 5,
                     verbose: bool = True) -> list[MMBacktestResult]:
        """
        Walk-forward validation: split data into sequential chunks,
        test each independently. If strategy only profits in 1-2 splits,
        the profitable period was likely luck.
        """
        split_size = len(self.candles) // n_splits
        results = []

        if verbose:
            print(f"\n🔬 WALK-FORWARD VALIDATION ({n_splits} splits, "
                  f"{split_size} candles each)")
            print("=" * 80)

        for i in range(n_splits):
            start = i * split_size
            end = start + split_size
            split_candles = self.candles[start:end]

            sub_bt = ImprovedMMBacktester(split_candles)
            r = sub_bt.run(cfg, f"split_{i+1}")
            results.append(r)

            if verbose:
                start_date = datetime.fromtimestamp(split_candles[0]["timestamp"])
                end_date = datetime.fromtimestamp(split_candles[-1]["timestamp"])
                color = "✅" if r.total_pnl > 0 else "❌"
                print(f"  {color} Split {i+1}: {start_date:%m/%d} → {end_date:%m/%d} | "
                      f"P&L: ${r.total_pnl:+8.2f} | Sharpe: {r.sharpe_ratio:+6.2f} | "
                      f"Fills: {r.total_fills}")

        profitable = sum(1 for r in results if r.total_pnl > 0)
        if verbose:
            print(f"\n  Profitable splits: {profitable}/{n_splits}")
            if profitable >= n_splits * 0.6:
                print("  ✅ Strategy appears robust across time periods")
            elif profitable >= n_splits * 0.4:
                print("  ⚠️  Mixed results — strategy may be fragile")
            else:
                print("  ❌ Strategy fails in most periods — likely overfit")

        return results

    def monte_carlo(self, cfg: MMBacktestConfig, n_runs: int = 200,
                    verbose: bool = True) -> dict:
        """
        Monte Carlo resampling: randomly shuffle market windows
        and re-run the backtest multiple times. This gives confidence
        intervals on the P&L distribution.
        """
        if verbose:
            print(f"\n🎲 MONTE CARLO SIMULATION ({n_runs} runs)")
            print("=" * 80)

        pnls = []
        sharpes = []
        drawdowns = []
        market_size = cfg.market_duration_seconds // 60

        for run in range(n_runs):
            # Resample candles in chunks (preserve intra-market structure)
            n_chunks = len(self.candles) // market_size
            indices = [random.randint(0, n_chunks-1) for _ in range(n_chunks)]
            resampled = []
            for idx in indices:
                start = idx * market_size
                end = start + market_size
                resampled.extend(self.candles[start:end])

            sub_bt = ImprovedMMBacktester(resampled)
            r = sub_bt.run(cfg, f"mc_{run}", verbose=False)
            pnls.append(r.total_pnl)
            sharpes.append(r.sharpe_ratio)
            drawdowns.append(r.max_drawdown)

            if verbose and (run + 1) % 50 == 0:
                print(f"  Progress: {run+1}/{n_runs}")

        # Compute percentiles
        pnls.sort()
        n = len(pnls)

        result = {
            "median_pnl": pnls[n // 2],
            "p5_pnl": pnls[int(n * 0.05)],
            "p25_pnl": pnls[int(n * 0.25)],
            "p75_pnl": pnls[int(n * 0.75)],
            "p95_pnl": pnls[int(n * 0.95)],
            "mean_pnl": sum(pnls) / n,
            "std_pnl": statistics.stdev(pnls) if n > 1 else 0,
            "prob_profitable": sum(1 for p in pnls if p > 0) / n,
            "median_sharpe": sorted(sharpes)[n // 2],
            "median_drawdown": sorted(drawdowns)[n // 2],
            "worst_drawdown": max(drawdowns),
        }

        if verbose:
            print(f"\n  Results across {n_runs} simulations:")
            print(f"  ┌─────────────────────────────────────┐")
            print(f"  │ Median P&L:    ${result['median_pnl']:+8.2f}            │")
            print(f"  │ 5th percentile: ${result['p5_pnl']:+8.2f} (worst case) │")
            print(f"  │ 95th percentile:${result['p95_pnl']:+8.2f} (best case)  │")
            print(f"  │ P(profitable):  {result['prob_profitable']*100:5.1f}%             │")
            print(f"  │ Median Sharpe:  {result['median_sharpe']:+6.2f}              │")
            print(f"  │ Worst Drawdown: ${result['worst_drawdown']:8.2f}          │")
            print(f"  └─────────────────────────────────────┘")

        return result


# ═══════════════════════════════════════════════════════════
#  GRID OPTIMIZER
# ═══════════════════════════════════════════════════════════

def run_grid_optimization(candles: list[dict], verbose: bool = True) -> list[dict]:
    """
    Grid search over key parameters to find optimal combination.
    Ranked by Sharpe ratio (risk-adjusted return).
    """
    bt = ImprovedMMBacktester(candles)

    spread_values = [0.02, 0.03, 0.04, 0.05, 0.06]
    vol_mult_values = [3.0, 5.0, 8.0]
    skew_values = [0.001, 0.002, 0.004]
    size_values = [10.0, 20.0, 30.0]

    total = len(spread_values) * len(vol_mult_values) * len(skew_values) * len(size_values)

    if verbose:
        print(f"\n🔬 GRID OPTIMIZATION ({total} combinations)")
        print("=" * 80)

    results = []
    count = 0

    for spread, vm, skew, size in cart_product(spread_values, vol_mult_values,
                                                 skew_values, size_values):
        count += 1
        if verbose and count % 20 == 0:
            print(f"  Progress: {count}/{total} ({count/total*100:.0f}%)")

        cfg = MMBacktestConfig(
            base_spread=spread,
            volatility_multiplier=vm,
            skew_factor=skew,
            quote_size=size,
        )

        try:
            r = bt.run(cfg, verbose=False)
            if r.total_fills < 50:
                continue  # Too few fills to be meaningful

            results.append({
                "spread": spread,
                "vol_mult": vm,
                "skew": skew,
                "size": size,
                "total_pnl": r.total_pnl,
                "sharpe": r.sharpe_ratio,
                "max_drawdown": r.max_drawdown,
                "fills": r.total_fills,
                "win_rate": r.win_rate,
                "pnl_per_fill": r.pnl_per_fill,
                "calmar": r.calmar_ratio,
            })
        except Exception:
            continue

    # Sort by Sharpe
    results.sort(key=lambda r: r["sharpe"], reverse=True)

    if verbose and results:
        print(f"\n{'═' * 90}")
        print(f"  TOP 10 PARAMETER COMBINATIONS (by Sharpe)")
        print(f"{'═' * 90}")
        print(f"  {'Spread':>7} {'VMult':>6} {'Skew':>6} {'Size':>5} | "
              f"{'P&L':>8} {'Sharpe':>7} {'MaxDD':>7} {'Fills':>6} {'WR%':>5}")
        print(f"  {'─'*7} {'─'*6} {'─'*6} {'─'*5} | {'─'*8} {'─'*7} {'─'*7} {'─'*6} {'─'*5}")

        for r in results[:10]:
            print(f"  {r['spread']:7.2f} {r['vol_mult']:6.1f} {r['skew']:6.3f} "
                  f"{r['size']:5.0f} | ${r['total_pnl']:+7.2f} {r['sharpe']:+7.2f} "
                  f"${r['max_drawdown']:6.2f} {r['fills']:6d} {r['win_rate']*100:4.1f}%")

    return results


# ═══════════════════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Improved MM Backtester")
    parser.add_argument("--days", type=int, default=7,
                        help="Days of historical data (default: 7)")
    parser.add_argument("--optimize", action="store_true",
                        help="Run grid optimization")
    parser.add_argument("--walk-forward", action="store_true",
                        help="Run walk-forward validation")
    parser.add_argument("--splits", type=int, default=5,
                        help="Number of walk-forward splits (default: 5)")
    parser.add_argument("--monte-carlo", action="store_true",
                        help="Run Monte Carlo simulation")
    parser.add_argument("--runs", type=int, default=200,
                        help="Number of Monte Carlo runs (default: 200)")
    parser.add_argument("--spread", type=float, default=0.04)
    parser.add_argument("--skew", type=float, default=0.002)
    parser.add_argument("--size", type=float, default=20.0)
    args = parser.parse_args()

    print("""
╔══════════════════════════════════════════════════════════════╗
║  IMPROVED MARKET MAKER BACKTESTER                            ║
║  Real data • Volume-aware fills • Walk-forward validation    ║
╚══════════════════════════════════════════════════════════════╝
    """)

    # Load data
    candles = load_cached_candles(days=args.days)
    if not candles:
        print("❌ No candle data available. Check your internet connection.")
        return

    bt = ImprovedMMBacktester(candles)

    if args.optimize:
        results = run_grid_optimization(candles)
        Path("data").mkdir(exist_ok=True)
        with open("data/mm_optimization_results.json", "w") as f:
            json.dump(results, f, indent=2)
        print(f"\n📁 Results saved to data/mm_optimization_results.json")

    elif args.walk_forward:
        cfg = MMBacktestConfig(
            base_spread=args.spread,
            skew_factor=args.skew,
            quote_size=args.size,
        )
        bt.walk_forward(cfg, n_splits=args.splits)

    elif args.monte_carlo:
        cfg = MMBacktestConfig(
            base_spread=args.spread,
            skew_factor=args.skew,
            quote_size=args.size,
        )
        result = bt.monte_carlo(cfg, n_runs=args.runs)
        Path("data").mkdir(exist_ok=True)
        with open("data/mm_monte_carlo.json", "w") as f:
            json.dump(result, f, indent=2)
        print(f"\n📁 Results saved to data/mm_monte_carlo.json")

    else:
        # Run default parameter sweep
        results = bt.run_parameter_sweep()

        # Find best
        best = max(results, key=lambda r: r.sharpe_ratio)
        print(f"\n{'═' * 70}")
        print(f"🏆 BEST CONFIGURATION: {best.config_name}")
        print(f"   Total P&L: ${best.total_pnl:+.2f}")
        print(f"   Sharpe Ratio: {best.sharpe_ratio:+.2f}")
        print(f"   Max Drawdown: ${best.max_drawdown:.2f}")
        print(f"   Fills: {best.total_fills} ({best.adverse_fills} adverse)")
        print(f"   Win Rate: {best.win_rate*100:.1f}%")
        print(f"{'═' * 70}")

        # Save
        Path("data").mkdir(exist_ok=True)
        with open("data/mm_backtest_results.json", "w") as f:
            json.dump([asdict(r) for r in results], f, indent=2)
        print(f"\n📁 Results saved to data/mm_backtest_results.json")


if __name__ == "__main__":
    main()