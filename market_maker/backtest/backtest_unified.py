"""
╔══════════════════════════════════════════════════════════════╗
║  UNIFIED MARKET MAKER BACKTESTER                             ║
║                                                              ║
║  Plugs the real EnhancedQuoteEngine and EnhancedFairValue-   ║
║  Engine from mm_enhanced1.py into the real-candle infra-     ║
║  structure from backtest_impV1.py.                           ║
║                                                              ║
║  Answers the question: "How does the ACTUAL quoting engine   ║
║  perform on REAL BTC price history?"                         ║
║                                                              ║
║  Usage (standalone):                                         ║
║    python backtests/backtest_unified.py --run                ║
║    python backtests/backtest_unified.py --walk-forward       ║
║    python backtests/backtest_unified.py --monte-carlo        ║
║    python backtests/backtest_unified.py --grid               ║
║                                                              ║
║  Usage (from mm_enhanced1.py):                               ║
║    python mm_enhanced1.py --backtest                         ║
╚══════════════════════════════════════════════════════════════╝
"""

import json
import math
import time
import random
import argparse
import statistics
import sys
import os
from collections import deque
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from itertools import product as cart_product
from typing import Optional

# ── Allow imports from parent directory ──────────────────────────────────────
_PARENT = Path(__file__).resolve().parent.parent
if str(_PARENT) not in sys.path:
    sys.path.insert(0, str(_PARENT))

try:
    import requests
except ImportError:
    print("❌ Missing: pip install requests")
    raise SystemExit(1)

# ── Import the real engines ───────────────────────────────────────────────────
from mm_enhanced1 import (
    SideDataSnapshot,
    EnhancedFairValueEngine,
    EnhancedQuoteEngine,
)
from fees import net_fill_fee, GAS_COST_PER_TX


# ═══════════════════════════════════════════════════════════
#  CONFIGURATION
# ═══════════════════════════════════════════════════════════

@dataclass
class UnifiedBacktestConfig:
    """Parameters for a single unified backtest run."""
    base_spread: float = 0.06
    max_spread: float = 0.12
    base_size: float = 10.0
    max_inventory: float = 100.0
    skew_factor: float = 0.002

    # Simulation settings
    num_markets: int = 0          # 0 = use all available 5-min windows
    market_duration_seconds: int = 300
    expiry_cutoff_seconds: float = 30.0

    # Fill model
    base_fill_rate: float = 0.08  # Base probability a quote is filled per tick

    # Fee model
    maker_fill_fraction: float = 0.80   # 80% of fills are resting (maker rebate)
    market_category: str = "crypto"


# ═══════════════════════════════════════════════════════════
#  RESULT
# ═══════════════════════════════════════════════════════════

@dataclass
class UnifiedBacktestResult:
    config_name: str = ""
    total_markets: int = 0
    total_fills: int = 0
    round_trips: int = 0
    adverse_fills: int = 0
    total_pnl: float = 0.0
    spread_pnl: float = 0.0
    fee_paid: float = 0.0
    rebate_earned: float = 0.0
    max_inventory: float = 0.0
    avg_spread: float = 0.0
    sharpe_ratio: float = 0.0
    max_drawdown: float = 0.0
    calmar_ratio: float = 0.0
    win_rate: float = 0.0
    pnl_per_market: float = 0.0


# ═══════════════════════════════════════════════════════════
#  CANDLE LOADER  (copied from backtest_impV1.py)
# ═══════════════════════════════════════════════════════════

def fetch_binance_candles(days: int = 7, symbol: str = "BTCUSDT") -> list[dict]:
    """Fetch real 1-minute BTC candles from Binance."""
    candles = []
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - (days * 86400 * 1000)

    print(f"📡 Fetching {days} days of {symbol} 1m candles from Binance...")

    current = start_ms
    while current < end_ms:
        try:
            r = requests.get(
                "https://api.binance.com/api/v3/klines",
                params={"symbol": symbol, "interval": "1m",
                        "startTime": current, "limit": 1000},
                timeout=10,
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
            current = int(data[-1][0]) + 60000
            time.sleep(0.15)
        except Exception as e:
            print(f"  ⚠️ Fetch error at {datetime.fromtimestamp(current/1000)}: {e}")
            current += 60000 * 100
            time.sleep(1)

    print(f"  ✅ Loaded {len(candles)} candles ({len(candles)/1440:.1f} days)")
    return candles


def load_cached_candles(filepath: str = "data/btc_candles.json",
                        days: int = 7) -> list[dict]:
    """Load from 24h cache or fetch fresh."""
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
#  CONTRACT PRICING  (from backtest_impV1.py)
# ═══════════════════════════════════════════════════════════

def btc_to_contract_price(btc_price: float, strike: float,
                           seconds_left: float,
                           btc_vol_per_second: float) -> float:
    """
    Convert BTC price to YES contract probability using a simplified
    Black-Scholes-like model.
    """
    if seconds_left <= 0:
        return 1.0 if btc_price > strike else 0.0
    if btc_vol_per_second <= 0:
        btc_vol_per_second = 0.0001
    z = (btc_price - strike) / (strike * btc_vol_per_second * math.sqrt(seconds_left))
    prob = 1.0 / (1.0 + math.exp(-1.7 * z))
    return max(0.02, min(0.98, prob))


# ═══════════════════════════════════════════════════════════
#  FILL SIMULATOR  (from backtest_impV1.py, unchanged)
# ═══════════════════════════════════════════════════════════

class FillSimulator:
    def __init__(self, base_fill_rate: float = 0.08):
        self.base_fill_rate = base_fill_rate

    def fill_probability(self, spread: float, volume_ratio: float,
                          volatility: float, seconds_left: float) -> float:
        spread_factor = max(0.2, 0.04 / max(spread, 0.01))
        vol_factor = min(2.0, max(0.3, volume_ratio))
        volatility_factor = min(2.0, max(0.5, 1.0 + volatility * 100))
        expiry_factor = 1.0
        if seconds_left < 60:
            expiry_factor = 1.0 + (60 - seconds_left) / 60 * 0.5
        prob = self.base_fill_rate * spread_factor * vol_factor * volatility_factor * expiry_factor
        return min(0.4, prob)

    def is_adverse(self, volatility: float, momentum_abs: float) -> bool:
        base = 0.20
        vol_adj = min(0.30, volatility * 50)
        mom_adj = min(0.20, momentum_abs * 20)
        return random.random() < (base + vol_adj + mom_adj)


# ═══════════════════════════════════════════════════════════
#  SNAPSHOT BUILDER
#  Converts a rolling OHLCV candle window → SideDataSnapshot
# ═══════════════════════════════════════════════════════════

class SnapshotBuilder:
    """
    Derives SideDataSnapshot fields from a sliding window of 1-minute candles.

    The primary drivers (btc_price, btc_change_1m, btc_change_5m,
    btc_volatility_1m, chainlink_price) come directly from real Binance
    data. Side signals (cvd, funding, liq) are derived proxies that add
    realistic noise without distorting the main price signal.
    """

    def __init__(self):
        self._candles: deque[dict] = deque(maxlen=10)

    def push(self, candle: dict) -> None:
        self._candles.append(candle)

    def build(self, current_contract_price: float,
              seconds_to_expiry: float) -> SideDataSnapshot:
        if not self._candles:
            return SideDataSnapshot(
                market_best_bid=current_contract_price - 0.005,
                market_best_ask=current_contract_price + 0.005,
                market_spread=0.01,
                seconds_to_expiry=seconds_to_expiry,
                timestamp=time.time(),
            )

        candles = list(self._candles)
        latest = candles[-1]
        btc_price = latest["close"]

        # ── 1m change ──
        btc_change_1m = 0.0
        if len(candles) >= 2:
            prev = candles[-2]["close"]
            btc_change_1m = (btc_price - prev) / prev if prev > 0 else 0.0

        # ── 5m change ──
        btc_change_5m = 0.0
        if len(candles) >= 6:
            prev5 = candles[-6]["close"]
            btc_change_5m = (btc_price - prev5) / prev5 if prev5 > 0 else 0.0
        elif len(candles) >= 2:
            btc_change_5m = btc_change_1m

        # ── 1m volatility (std dev of last 5 1m returns) ──
        btc_vol = 0.001
        if len(candles) >= 3:
            closes = [c["close"] for c in candles[-6:]]
            rets = [(closes[i] - closes[i-1]) / closes[i-1]
                    for i in range(1, len(closes)) if closes[i-1] > 0]
            if len(rets) >= 2:
                btc_vol = statistics.stdev(rets)

        # ── CVD signal: volume-weighted directional consensus over 5 candles ──
        cvd_signal = 0.0
        window = candles[-5:]
        total_vol = sum(c["volume"] for c in window)
        if total_vol > 0:
            weighted_dir = sum(
                (1.0 if c["close"] >= c["open"] else -1.0) * c["volume"]
                for c in window
            )
            cvd_signal = max(-1.0, min(1.0, weighted_dir / total_vol))

        # ── Funding proxy: tanh of 5m momentum ──
        funding_signal = math.tanh(btc_change_5m / 0.005)
        hl_funding_rate = funding_signal * 0.0001

        # ── Liquidation proxy: flag sharp 1m moves with volume spike ──
        liq_signal = 0.0
        if len(candles) >= 3:
            avg_vol = sum(c["volume"] for c in candles[:-1]) / max(len(candles) - 1, 1)
            spike = latest["volume"] > avg_vol * 1.5
            if spike and abs(btc_change_1m) > 0.003:
                mag = min(1.0, abs(btc_change_1m) / 0.01)
                liq_signal = math.copysign(mag, btc_change_1m)

        # ── Market microstructure (synthetic ±0.5¢ around contract mid) ──
        bid = max(0.01, current_contract_price - 0.005)
        ask = min(0.99, current_contract_price + 0.005)

        return SideDataSnapshot(
            btc_price=btc_price,
            btc_change_1m=btc_change_1m,
            btc_change_5m=btc_change_5m,
            btc_volatility_1m=btc_vol,
            hl_oracle_price=btc_price,       # same as spot in backtest
            hl_funding_rate=hl_funding_rate,
            hl_open_interest=0.0,
            cvd_signal=cvd_signal,
            liq_signal=liq_signal,
            funding_signal=funding_signal,
            oi_signal=0.0,                   # not derivable from candles
            chainlink_price=btc_price,       # on-chain = spot in backtest
            market_spread=ask - bid,
            market_best_bid=bid,
            market_best_ask=ask,
            seconds_to_expiry=seconds_to_expiry,
            timestamp=latest["timestamp"],
        )


# ═══════════════════════════════════════════════════════════
#  UNIFIED BACKTESTER
# ═══════════════════════════════════════════════════════════

class UnifiedMMBacktester:
    """
    Backtests EnhancedQuoteEngine + EnhancedFairValueEngine against
    real Binance 1-minute candles.

    Each 5-minute window in the candle data is treated as one
    Polymarket BTC UP/DOWN market. Within each window, each
    1-minute candle is interpolated to 60 ticks.
    """

    def __init__(self, candles: list[dict]):
        self.candles = candles
        self.fill_sim = FillSimulator()

    def run(self, cfg: UnifiedBacktestConfig,
            config_name: str = "default",
            verbose: bool = False) -> UnifiedBacktestResult:

        fv_engine = EnhancedFairValueEngine()
        quote_engine = EnhancedQuoteEngine(
            base_spread=cfg.base_spread,
            max_spread=cfg.max_spread,
            base_size=cfg.base_size,
            max_inventory=cfg.max_inventory,
            skew_factor=cfg.skew_factor,
        )

        market_size = cfg.market_duration_seconds // 60   # 5 candles per market
        total_markets = len(self.candles) // market_size
        if cfg.num_markets > 0:
            total_markets = min(total_markets, cfg.num_markets)

        # State
        net_inventory = 0.0
        total_fills = 0
        adverse_fills = 0
        round_trips = 0
        spread_pnl = 0.0
        running_pnl = 0.0
        fee_paid = 0.0
        rebate_earned = 0.0
        max_inv = 0.0
        peak_pnl = 0.0
        max_drawdown = 0.0
        spreads: list[float] = []
        market_pnls: list[float] = []
        buy_queue: list[float] = []

        # Rolling stats across the whole history (not reset per market)
        price_history: deque[float] = deque(maxlen=300)
        volume_history: deque[float] = deque(maxlen=300)

        for market_idx in range(total_markets):
            start_idx = market_idx * market_size
            market_candles = self.candles[start_idx: start_idx + market_size]
            if len(market_candles) < market_size:
                break

            open_price = market_candles[0]["open"]
            strike = open_price    # "Will BTC close above its open?"
            market_start_pnl = running_pnl
            snapshot_builder = SnapshotBuilder()

            for candle_idx, candle in enumerate(market_candles):
                snapshot_builder.push(candle)
                ticks_per_candle = 60
                price_range = candle["high"] - candle["low"]

                for tick in range(ticks_per_candle):
                    # Interpolate BTC price within candle
                    if tick == 0:
                        btc_price = candle["open"]
                    elif tick == ticks_per_candle - 1:
                        btc_price = candle["close"]
                    else:
                        btc_price = candle["low"] + random.random() * price_range

                    elapsed = candle_idx * 60 + tick
                    seconds_left = cfg.market_duration_seconds - elapsed

                    if seconds_left <= cfg.expiry_cutoff_seconds:
                        break

                    # Rolling stats
                    price_history.append(btc_price)
                    volume_history.append(candle["volume"] / 60)

                    # Volatility for contract pricing
                    if len(price_history) >= 10:
                        rets = [
                            (price_history[i] - price_history[i-1]) / price_history[i-1]
                            for i in range(1, len(price_history))
                        ]
                        vol_for_pricing = statistics.stdev(rets) if len(rets) > 1 else 0.001
                    else:
                        vol_for_pricing = 0.001

                    # Contract fair value from Black-Scholes proxy
                    contract_price = btc_to_contract_price(
                        btc_price, strike, seconds_left,
                        max(vol_for_pricing, 0.0001)
                    )

                    # Build snapshot from candle window
                    snapshot = snapshot_builder.build(contract_price, seconds_left)

                    # Fair value estimate from real engine
                    market_yes_price = (snapshot.market_best_bid + snapshot.market_best_ask) / 2
                    fair_value = fv_engine.estimate(snapshot, market_yes_price)

                    # Quotes from real engine
                    quotes = quote_engine.generate_quotes(fair_value, net_inventory, snapshot)
                    spreads.append(quotes["spread"])

                    # Inventory limit check
                    if abs(net_inventory) >= cfg.max_inventory:
                        continue

                    # Volume ratio
                    avg_vol = sum(volume_history) / max(len(volume_history), 1)
                    vol_ratio = (candle["volume"] / 60) / max(avg_vol, 0.01)

                    fill_prob = self.fill_sim.fill_probability(
                        quotes["spread"], vol_ratio,
                        snapshot.btc_volatility_1m, seconds_left
                    )

                    size = quotes["size"]
                    is_adverse = self.fill_sim.is_adverse(
                        snapshot.btc_volatility_1m,
                        abs(snapshot.btc_change_1m)
                    )

                    # ── Bid fill (someone sells YES to us — we BUY) ──
                    if random.random() < fill_prob:
                        total_fills += 1
                        buy_price = quotes["yes_bid"]
                        net_inventory += size
                        max_inv = max(max_inv, abs(net_inventory))
                        buy_queue.append(buy_price)

                        is_maker = random.random() < cfg.maker_fill_fraction
                        fee = net_fill_fee(buy_price, size, is_maker=is_maker,
                                           category=cfg.market_category)
                        running_pnl -= fee
                        if fee > 0:
                            fee_paid += fee
                        else:
                            rebate_earned += abs(fee)

                        if is_adverse:
                            adverse_fills += 1
                            # Adverse: informed trader hits our bid — price moves down
                            running_pnl -= size * quotes["spread"] * 0.3

                    # ── Ask fill (someone buys YES from us — we SELL) ──
                    if random.random() < fill_prob and buy_queue:
                        total_fills += 1
                        sell_price = quotes["yes_ask"]
                        net_inventory -= size

                        buy_price = buy_queue.pop(0)
                        trip_pnl = (sell_price - buy_price) * size
                        spread_pnl += trip_pnl
                        running_pnl += trip_pnl
                        round_trips += 1

                        is_maker = random.random() < cfg.maker_fill_fraction
                        fee = net_fill_fee(sell_price, size, is_maker=is_maker,
                                           category=cfg.market_category)
                        running_pnl -= fee
                        if fee > 0:
                            fee_paid += fee
                        else:
                            rebate_earned += abs(fee)

                    # Drawdown tracking
                    peak_pnl = max(peak_pnl, running_pnl)
                    max_drawdown = max(max_drawdown, peak_pnl - running_pnl)

            # End of market: close remaining inventory at mid (haircut)
            if abs(net_inventory) > 0:
                running_pnl -= abs(net_inventory) * cfg.base_spread * 0.5
                net_inventory = 0.0
                buy_queue.clear()

            market_pnls.append(running_pnl - market_start_pnl)

        # ── Summary statistics ──
        n = len(market_pnls)
        if n > 1:
            avg_ret = sum(market_pnls) / n
            std_ret = statistics.stdev(market_pnls)
            sharpe = (avg_ret / std_ret) * math.sqrt(252 * 288) if std_ret > 0 else 0.0
        else:
            sharpe = 0.0

        win_rate = sum(1 for p in market_pnls if p > 0) / max(n, 1)

        calmar = 0.0
        if max_drawdown > 0 and running_pnl > 0:
            annual_return = running_pnl * (365 * 288 / max(n, 1))
            calmar = annual_return / max_drawdown

        return UnifiedBacktestResult(
            config_name=config_name,
            total_markets=total_markets,
            total_fills=total_fills,
            round_trips=round_trips,
            adverse_fills=adverse_fills,
            total_pnl=round(running_pnl, 4),
            spread_pnl=round(spread_pnl, 4),
            fee_paid=round(fee_paid, 4),
            rebate_earned=round(rebate_earned, 4),
            max_inventory=round(max_inv, 1),
            avg_spread=round(sum(spreads) / max(len(spreads), 1), 4),
            sharpe_ratio=round(sharpe, 2),
            max_drawdown=round(max_drawdown, 4),
            calmar_ratio=round(calmar, 2),
            win_rate=round(win_rate, 4),
            pnl_per_market=round(running_pnl / max(n, 1), 4),
        )

    def walk_forward(self, cfg: UnifiedBacktestConfig, n_splits: int = 5,
                     verbose: bool = True) -> list[UnifiedBacktestResult]:
        """
        Walk-forward validation: split data into sequential chunks, test each
        independently. Robust strategies profit in most splits.
        """
        split_size = len(self.candles) // n_splits
        results = []

        if verbose:
            print(f"\n🔬 WALK-FORWARD VALIDATION ({n_splits} splits, "
                  f"{split_size} candles each)")
            print("=" * 80)

        for i in range(n_splits):
            start = i * split_size
            split_candles = self.candles[start: start + split_size]
            sub_bt = UnifiedMMBacktester(split_candles)
            r = sub_bt.run(cfg, f"split_{i+1}")
            results.append(r)

            if verbose:
                start_dt = datetime.fromtimestamp(split_candles[0]["timestamp"])
                end_dt = datetime.fromtimestamp(split_candles[-1]["timestamp"])
                icon = "✅" if r.total_pnl > 0 else "❌"
                print(f"  {icon} Split {i+1}: {start_dt:%m/%d} → {end_dt:%m/%d} | "
                      f"P&L: ${r.total_pnl:+8.2f} | Sharpe: {r.sharpe_ratio:+6.2f} | "
                      f"Fills: {r.total_fills}")

        if verbose:
            profitable = sum(1 for r in results if r.total_pnl > 0)
            print(f"\n  Profitable splits: {profitable}/{n_splits}")
            if profitable >= n_splits * 0.6:
                print("  ✅ Strategy appears robust across time periods")
            elif profitable >= n_splits * 0.4:
                print("  ⚠️  Mixed results — strategy may be fragile")
            else:
                print("  ❌ Strategy fails in most periods — likely overfit")

        return results

    def monte_carlo(self, cfg: UnifiedBacktestConfig, n_runs: int = 200,
                    verbose: bool = True) -> dict:
        """
        Monte Carlo resampling: randomly shuffle 5-minute market windows
        and re-run the backtest multiple times for confidence intervals.
        """
        if verbose:
            print(f"\n🎲 MONTE CARLO SIMULATION ({n_runs} runs)")
            print("=" * 80)

        market_size = cfg.market_duration_seconds // 60
        n_chunks = len(self.candles) // market_size

        pnls: list[float] = []
        sharpes: list[float] = []
        drawdowns: list[float] = []

        for run in range(n_runs):
            indices = [random.randint(0, n_chunks - 1) for _ in range(n_chunks)]
            resampled: list[dict] = []
            for idx in indices:
                s = idx * market_size
                resampled.extend(self.candles[s: s + market_size])

            sub_bt = UnifiedMMBacktester(resampled)
            r = sub_bt.run(cfg, f"mc_{run}", verbose=False)
            pnls.append(r.total_pnl)
            sharpes.append(r.sharpe_ratio)
            drawdowns.append(r.max_drawdown)

            if verbose and (run + 1) % 50 == 0:
                print(f"  Progress: {run+1}/{n_runs}")

        pnls.sort()
        n = len(pnls)

        result = {
            "median_pnl":       pnls[n // 2],
            "p5_pnl":           pnls[int(n * 0.05)],
            "p25_pnl":          pnls[int(n * 0.25)],
            "p75_pnl":          pnls[int(n * 0.75)],
            "p95_pnl":          pnls[int(n * 0.95)],
            "mean_pnl":         sum(pnls) / n,
            "std_pnl":          statistics.stdev(pnls) if n > 1 else 0,
            "prob_profitable":  sum(1 for p in pnls if p > 0) / n,
            "median_sharpe":    sorted(sharpes)[n // 2],
            "median_drawdown":  sorted(drawdowns)[n // 2],
            "worst_drawdown":   max(drawdowns),
        }

        if verbose:
            print(f"\n  Results across {n_runs} simulations:")
            print(f"  ┌─────────────────────────────────────────┐")
            print(f"  │ Median P&L:      ${result['median_pnl']:+8.2f}              │")
            print(f"  │ 5th percentile:  ${result['p5_pnl']:+8.2f} (worst case)   │")
            print(f"  │ 95th percentile: ${result['p95_pnl']:+8.2f} (best case)    │")
            print(f"  │ P(profitable):   {result['prob_profitable']*100:5.1f}%                │")
            print(f"  │ Median Sharpe:   {result['median_sharpe']:+6.2f}                │")
            print(f"  │ Worst Drawdown:  ${result['worst_drawdown']:8.2f}              │")
            print(f"  └─────────────────────────────────────────┘")

        return result


# ═══════════════════════════════════════════════════════════
#  GRID OPTIMIZER  — tuned to EnhancedQuoteEngine params
# ═══════════════════════════════════════════════════════════

# 5 × 4 × 4 × 3 = 240 combinations
_GRID = {
    "base_spread": [0.04, 0.05, 0.06, 0.07, 0.08],
    "skew_factor": [0.0,  0.001, 0.002, 0.004],
    "base_size":   [5.0, 10.0, 20.0, 30.0],
    "max_spread":  [0.10, 0.12, 0.15],
}


def run_grid_optimization(candles: list[dict],
                           verbose: bool = True) -> list[dict]:
    """Grid search over EnhancedQuoteEngine parameters. Ranked by Sharpe."""
    bt = UnifiedMMBacktester(candles)
    combos = list(cart_product(
        _GRID["base_spread"],
        _GRID["skew_factor"],
        _GRID["base_size"],
        _GRID["max_spread"],
    ))
    total = len(combos)

    if verbose:
        print(f"\n🔬 GRID OPTIMIZATION ({total} combinations)")
        print("=" * 80)

    results = []
    for count, (spread, skew, size, max_sp) in enumerate(combos, 1):
        if verbose and count % 30 == 0:
            print(f"  Progress: {count}/{total} ({count/total*100:.0f}%)")

        cfg = UnifiedBacktestConfig(
            base_spread=spread,
            skew_factor=skew,
            base_size=size,
            max_spread=max_sp,
        )

        try:
            r = bt.run(cfg, verbose=False)
            if r.total_fills < 30:
                continue
            results.append({
                "base_spread": spread,
                "skew_factor": skew,
                "base_size": size,
                "max_spread": max_sp,
                "total_pnl": r.total_pnl,
                "sharpe": r.sharpe_ratio,
                "max_drawdown": r.max_drawdown,
                "fills": r.total_fills,
                "win_rate": r.win_rate,
                "pnl_per_market": r.pnl_per_market,
                "calmar": r.calmar_ratio,
            })
        except Exception:
            continue

    results.sort(key=lambda r: r["sharpe"], reverse=True)

    if verbose and results:
        print(f"\n{'═' * 95}")
        print(f"  TOP 10 PARAMETER COMBINATIONS (by Sharpe)")
        print(f"{'═' * 95}")
        print(f"  {'BSpread':>7} {'Skew':>6} {'Size':>5} {'MaxSp':>6} | "
              f"{'P&L':>8} {'Sharpe':>7} {'MaxDD':>7} {'Fills':>6} {'WR%':>5}")
        print(f"  {'─'*7} {'─'*6} {'─'*5} {'─'*6} | {'─'*8} {'─'*7} {'─'*7} {'─'*6} {'─'*5}")
        for r in results[:10]:
            print(f"  {r['base_spread']:7.3f} {r['skew_factor']:6.3f} "
                  f"{r['base_size']:5.0f} {r['max_spread']:6.3f} | "
                  f"${r['total_pnl']:+7.2f} {r['sharpe']:+7.2f} "
                  f"${r['max_drawdown']:6.2f} {r['fills']:6d} "
                  f"{r['win_rate']*100:4.1f}%")

    return results


# ═══════════════════════════════════════════════════════════
#  ENTRY POINT  (also called from mm_enhanced1.py --backtest)
# ═══════════════════════════════════════════════════════════

def run_unified_backtest(days: int = 7) -> None:
    """
    Full pipeline:
      1. Load real Binance candles (with 24h cache)
      2. Run grid optimization
      3. Run walk-forward on the best config
      4. Run Monte Carlo on the best config
    """
    print("""
╔══════════════════════════════════════════════════════════════╗
║  UNIFIED MARKET MAKER BACKTESTER                             ║
║  Real candles · EnhancedQuoteEngine · fees.py cost model     ║
╚══════════════════════════════════════════════════════════════╝
    """)

    # Resolve data path relative to project root (parent of backtests/)
    data_dir = Path(__file__).resolve().parent.parent / "data"
    candle_path = str(data_dir / "btc_candles.json")

    candles = load_cached_candles(filepath=candle_path, days=days)
    if not candles:
        print("❌ No candle data available. Check your internet connection.")
        return

    # ── Grid optimization ──
    grid_results = run_grid_optimization(candles)

    data_dir.mkdir(exist_ok=True)
    with open(data_dir / "unified_grid_results.json", "w") as f:
        json.dump(grid_results, f, indent=2)
    print(f"\n📁 Grid results saved to data/unified_grid_results.json")

    if not grid_results:
        print("⚠️  No grid results with enough fills — done.")
        return

    best_grid = grid_results[0]
    best_cfg = UnifiedBacktestConfig(
        base_spread=best_grid["base_spread"],
        skew_factor=best_grid["skew_factor"],
        base_size=best_grid["base_size"],
        max_spread=best_grid["max_spread"],
    )
    print(f"\n🏆 Best config: spread={best_cfg.base_spread:.3f} "
          f"skew={best_cfg.skew_factor:.3f} "
          f"size={best_cfg.base_size:.0f} "
          f"max_spread={best_cfg.max_spread:.3f}")
    print(f"   Sharpe={best_grid['sharpe']:+.2f}  "
          f"P&L=${best_grid['total_pnl']:+.2f}  "
          f"WR={best_grid['win_rate']*100:.1f}%")

    # ── Walk-forward ──
    bt = UnifiedMMBacktester(candles)
    bt.walk_forward(best_cfg, n_splits=5)

    # ── Monte Carlo ──
    mc_result = bt.monte_carlo(best_cfg, n_runs=200)

    with open(data_dir / "unified_monte_carlo.json", "w") as f:
        json.dump(mc_result, f, indent=2)
    print(f"\n📁 Monte Carlo results saved to data/unified_monte_carlo.json")


def main():
    parser = argparse.ArgumentParser(description="Unified MM Backtester")
    parser.add_argument("--days", type=int, default=7,
                        help="Days of historical data (default: 7)")
    parser.add_argument("--run", action="store_true",
                        help="Full pipeline: grid + walk-forward + Monte Carlo")
    parser.add_argument("--walk-forward", action="store_true",
                        help="Walk-forward only (uses default config)")
    parser.add_argument("--monte-carlo", action="store_true",
                        help="Monte Carlo only (uses default config)")
    parser.add_argument("--grid", action="store_true",
                        help="Grid optimization only")
    parser.add_argument("--spread", type=float, default=0.06)
    parser.add_argument("--skew", type=float, default=0.002)
    parser.add_argument("--size", type=float, default=10.0)
    parser.add_argument("--max-spread", type=float, default=0.12)
    args = parser.parse_args()

    data_dir = Path(__file__).resolve().parent.parent / "data"
    candle_path = str(data_dir / "btc_candles.json")
    candles = load_cached_candles(filepath=candle_path, days=args.days)
    if not candles:
        print("❌ No candle data available.")
        return

    cfg = UnifiedBacktestConfig(
        base_spread=args.spread,
        skew_factor=args.skew,
        base_size=args.size,
        max_spread=args.max_spread,
    )

    bt = UnifiedMMBacktester(candles)

    if args.grid:
        results = run_grid_optimization(candles)
        data_dir.mkdir(exist_ok=True)
        with open(data_dir / "unified_grid_results.json", "w") as f:
            json.dump(results, f, indent=2)
        print(f"\n📁 Saved to data/unified_grid_results.json")

    elif getattr(args, "walk_forward", False):
        bt.walk_forward(cfg)

    elif getattr(args, "monte_carlo", False):
        result = bt.monte_carlo(cfg, n_runs=200)
        data_dir.mkdir(exist_ok=True)
        with open(data_dir / "unified_monte_carlo.json", "w") as f:
            json.dump(result, f, indent=2)
        print(f"\n📁 Saved to data/unified_monte_carlo.json")

    else:
        # --run or bare invocation → full pipeline
        run_unified_backtest(days=args.days)


if __name__ == "__main__":
    main()
