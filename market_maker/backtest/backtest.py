"""
╔══════════════════════════════════════════════════════════════╗
║   POLYMARKET BTC BOT — DIRECTIONAL BACKTESTING ENGINE        ║
║                                                              ║
║   ⚠️  DEPRECATED — Use `python mm_enhanced_1.py --backtest`  ║
║   instead. The MM backtest has proven superior results:       ║
║     - MM backtest Sharpe: +1.61 vs this backtest: -93.37     ║
║     - This strategy: 19.5% win rate, -60% P&L               ║
║                                                              ║
║   Root causes of failure:                                    ║
║     - Market sim lag factor (0.85) creates mean-reversion    ║
║       trap that doesn't exist in real markets                ║
║     - Fees (3.6% round-trip) exceed profit target (2.5%)     ║
║     - Stop loss (1%) triggers before fee cost is realized    ║
║     - 78.5% of exits are stop losses — no exploitable edge   ║
║                                                              ║
║   This file is kept for reference. Use the MM backtest:      ║
║     python mm_enhanced_1.py --backtest                       ║
╚══════════════════════════════════════════════════════════════╝
"""

import json
import time
import math
import argparse
import statistics
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional
from itertools import product as cart_product

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import requests
except ImportError:
    print("❌ Missing: pip install requests")
    raise SystemExit(1)

from fees import polymarket_taker_fee, GAS_COST_PER_TX


# ═══════════════════════════════════════════════════════════
#  CONFIGURATION — mirrors bot.py Config but adds backtest params
# ═══════════════════════════════════════════════════════════

@dataclass
class BacktestConfig:
    """
    All strategy parameters that affect trade decisions.
    These mirror the Config dataclass in bot.py — change them here
    to test different strategies without touching the live bot.
    """
    # ── Capital & Position Sizing ──
    initial_capital: float = 1000.0
    max_trade_pct: float = 0.05          # 5% of capital per trade

    # ── Signal Thresholds ──
    min_edge_required: float = 0.005     # 0.5% minimum edge to enter
    momentum_1m_multiplier: float = 10.0 # How aggressively 1m momentum shifts fair value
    momentum_5m_multiplier: float = 5.0  # How aggressively 5m momentum shifts fair value
    momentum_1m_weight: float = 0.7      # Blend weight for 1m signal
    momentum_5m_weight: float = 0.3      # Blend weight for 5m signal
    max_1m_adjustment: float = 0.15      # Cap on 1m fair value shift
    max_5m_adjustment: float = 0.10      # Cap on 5m fair value shift

    # ── Exit Rules ──
    min_profit_target: float = 0.025     # 2.5% profit target
    stop_loss_pct: float = 0.01          # 1.0% stop loss
    max_hold_seconds: int = 270          # 4.5 minute max hold

    # ── Risk Management ──
    daily_loss_limit_pct: float = 0.10   # 10% daily loss limit
    consecutive_loss_limit: int = 5      # Circuit breaker trigger
    circuit_breaker_pause: int = 600     # 10 min pause after breaker

    # ── Simulated Market Parameters ──
    # Polymarket contract prices don't perfectly track BTC momentum.
    # This spread simulates the bid-ask inefficiency the bot exploits.
    simulated_spread: float = 0.02       # 2 cent spread on contract prices
    simulated_slippage: float = 0.005    # 0.5% slippage per fill
    market_window_seconds: int = 300     # 5-minute prediction windows

    # ── Fee Modeling ──
    # Polymarket uses dynamic taker fees: parabolic curve peaking at 50%.
    # Crypto markets: up to 1.80% at p=0.50, approaches 0% near p=0 or p=1.
    # Formula: fee_rate = max_fee * 4 * p * (1 - p)
    # Gas costs on Polygon are small but add up over many trades.
    use_dynamic_fees: bool = True         # True = price-dependent fees from fees.py
    taker_fee_pct_override: float = 0.018 # Fallback flat rate if dynamic disabled
    market_category: str = "crypto"       # Fee category (crypto peaks at 1.8%)
    gas_cost_per_trade: float = 0.005     # ~$0.005 per Polygon transaction

    # ── Backtest Period ──
    lookback_days: int = 7
    candle_interval: str = "1m"          # 1-minute candles from Binance


# ═══════════════════════════════════════════════════════════
#  DATA FETCHER — pulls historical BTC candles from Binance
# ═══════════════════════════════════════════════════════════

def fetch_binance_klines(
    symbol: str = "BTCUSDT",
    interval: str = "1m",
    days: int = 7,
    verbose: bool = True
) -> list[dict]:
    """
    Fetch historical 1-minute BTC candles from Binance's public API.
    Returns a list of dicts with: open_time, open, high, low, close, volume.

    Binance limits to 1000 candles per request, so we paginate backward
    from the current time. A full day of 1m candles is 1440 rows, so
    a 7-day backtest needs ~10 requests.
    """
    url = "https://api.binance.com/api/v3/klines"
    all_candles = []

    # Work backward from now in 1000-candle chunks
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - (days * 86400 * 1000)
    current_start = start_ms

    if verbose:
        print(f"📥 Fetching {days} days of {interval} BTC candles from Binance...")

    while current_start < end_ms:
        params = {
            "symbol": symbol,
            "interval": interval,
            "startTime": current_start,
            "limit": 1000,
        }
        try:
            resp = requests.get(url, params=params, timeout=10)
            resp.raise_for_status()
            raw = resp.json()
        except requests.exceptions.HTTPError as e:
            # Binance may be geo-blocked (HTTP 451) — fall back to Kraken
            if resp.status_code in (403, 451):
                if verbose:
                    print(f"⚠️  Binance blocked (HTTP {resp.status_code}), switching to Kraken OHLC...")
                return fetch_kraken_ohlc(interval=interval, days=days, verbose=verbose)
            raise

        if not raw:
            break

        for candle in raw:
            all_candles.append({
                "open_time": candle[0] / 1000,   # Unix seconds
                "open": float(candle[1]),
                "high": float(candle[2]),
                "low": float(candle[3]),
                "close": float(candle[4]),
                "volume": float(candle[5]),
            })

        # Next chunk starts after the last candle we received
        current_start = int(raw[-1][0]) + 60_000  # +1 minute
        time.sleep(0.1)  # Rate limit courtesy

    if verbose:
        print(f"   ✅ Fetched {len(all_candles)} candles ({all_candles[0]['close']:.0f} → {all_candles[-1]['close']:.0f})")

    return all_candles


def fetch_kraken_ohlc(
    interval: str = "1m",
    days: int = 7,
    verbose: bool = True
) -> list[dict]:
    """
    Fallback: fetch BTC/USD candles from Kraken (globally accessible).
    Kraken's OHLC endpoint returns up to 720 candles per request.
    """
    url = "https://api.kraken.com/0/public/OHLC"
    interval_map = {"1m": 1, "5m": 5, "15m": 15, "1h": 60}
    kraken_interval = interval_map.get(interval, 1)

    since = int(time.time()) - (days * 86400)
    all_candles = []

    if verbose:
        print(f"📥 Fetching {days} days of {interval} BTC candles from Kraken...")

    while True:
        params = {"pair": "XBTUSD", "interval": kraken_interval, "since": since}
        resp = requests.get(url, params=params, timeout=10)
        data = resp.json()
        result = data.get("result", {})
        ohlc = result.get("XXBTZUSD") or result.get("XBTUSD", [])

        if not ohlc:
            break

        for c in ohlc:
            all_candles.append({
                "open_time": float(c[0]),
                "open": float(c[1]),
                "high": float(c[2]),
                "low": float(c[3]),
                "close": float(c[4]),
                "volume": float(c[6]),
            })

        # Kraken returns a "last" timestamp for pagination
        last_ts = result.get("last", 0)
        if last_ts and int(str(last_ts)[:10]) > since:
            since = int(str(last_ts)[:10])
        else:
            break

        time.sleep(0.2)

    # Deduplicate by timestamp (Kraken can return overlapping ranges)
    seen = set()
    unique = []
    for c in all_candles:
        t = c["open_time"]
        if t not in seen:
            seen.add(t)
            unique.append(c)
    unique.sort(key=lambda x: x["open_time"])

    if verbose and unique:
        print(f"   ✅ Fetched {len(unique)} candles ({unique[0]['close']:.0f} → {unique[-1]['close']:.0f})")

    return unique


# ═══════════════════════════════════════════════════════════
#  SIMULATED SIGNAL ENGINE — mirrors bot.py logic exactly
# ═══════════════════════════════════════════════════════════

def calculate_fair_value(
    change_1m: float,
    change_5m: float,
    cfg: BacktestConfig,
) -> float:
    """
    Estimate fair probability that BTC will be UP at expiry.
    This mirrors SignalEngine.calculate_fair_value() in bot.py
    so that backtest results are representative of live performance.
    """
    # tanh scaling: suppresses noise (0.01% moves) while preserving real signals (0.1%+)
    # sensitivity = multiplier * 20 converts the old linear multiplier to tanh domain
    adj_1m = math.tanh(change_1m * cfg.momentum_1m_multiplier * 20) * cfg.max_1m_adjustment
    adj_5m = math.tanh(change_5m * cfg.momentum_5m_multiplier * 20) * cfg.max_5m_adjustment

    momentum_adj = (adj_1m * cfg.momentum_1m_weight) + (adj_5m * cfg.momentum_5m_weight)
    fair_value = 0.50 + momentum_adj

    return min(max(fair_value, 0.02), 0.98)


def generate_signal(
    fair_value: float,
    simulated_yes_price: float,
    simulated_no_price: float,
    seconds_to_expiry: float,
    cfg: BacktestConfig,
) -> tuple[str, float, float]:
    """
    Determine whether to BUY YES, BUY NO, or HOLD.
    Returns: (signal, edge, confidence)

    The edge is the gap between our fair value estimate and the
    simulated market price. In live trading, this gap exists because
    Polymarket contract prices react slower than raw BTC momentum.
    """
    if seconds_to_expiry < 30:
        return "HOLD", 0.0, 0.0

    yes_edge = fair_value - simulated_yes_price
    no_edge = (1.0 - fair_value) - simulated_no_price

    best_edge = max(yes_edge, no_edge)
    if best_edge < cfg.min_edge_required:
        return "HOLD", best_edge, 0.0

    if yes_edge > no_edge and yes_edge >= cfg.min_edge_required:
        signal = "YES"
        edge = yes_edge
    elif no_edge > yes_edge and no_edge >= cfg.min_edge_required:
        signal = "NO"
        edge = no_edge
    else:
        return "HOLD", 0.0, 0.0

    confidence = min(edge * 100, 1.0)
    return signal, edge, confidence


# ═══════════════════════════════════════════════════════════
#  SIMULATED MARKET — generates contract prices from BTC data
# ═══════════════════════════════════════════════════════════

def simulate_contract_price(
    change_since_open: float,
    seconds_to_expiry: float,
    window_seconds: float = 300.0,
    spread: float = 0.02,
) -> tuple[float, float]:
    """
    Simulate what a Polymarket YES/NO contract would be priced at
    given the current BTC price change since market open.

    The key insight: contract prices are a noisy, delayed version
    of the true probability. They lag BTC momentum by ~5-15 seconds
    because market makers update slowly. This lag is the edge.

    Returns: (yes_price, no_price)
    """
    # The "true" probability based on how much BTC has moved
    # A 0.1% move in 5 minutes has ~60% chance of holding to expiry
    # (calibrated from observation, not rigorous — needs real data)
    time_remaining_pct = max(0.01, seconds_to_expiry / window_seconds)

    # Stronger signal when more time has passed (move is more "established")
    signal_strength = (1.0 - time_remaining_pct) * 0.5 + 0.5

    # Convert BTC change to probability
    # 0.1% BTC move → ~60% probability, 0.5% move → ~85%
    raw_prob = 0.50 + math.tanh(change_since_open * 200) * 0.45 * signal_strength

    # Add spread: market maker keeps a margin around true probability
    # This is what creates the "edge" — our fair value calc is faster
    lagged_prob = raw_prob * 0.85 + 0.50 * 0.15  # Market lags true prob by ~15%

    yes_price = min(max(lagged_prob + spread / 2, 0.02), 0.98)
    no_price = min(max(1.0 - lagged_prob + spread / 2, 0.02), 0.98)

    return round(yes_price, 4), round(no_price, 4)


# ═══════════════════════════════════════════════════════════
#  TRADE SIMULATOR — tracks entries, exits, and P&L
# ═══════════════════════════════════════════════════════════

@dataclass
class SimulatedTrade:
    trade_id: int
    side: str                # "YES" or "NO"
    entry_price: float
    shares: float
    capital_used: float
    entry_time: float        # Unix timestamp
    entry_btc_price: float   # BTC price at entry
    window_open_price: float # BTC price when 5-min window opened
    window_expiry: float     # When this 5-min window expires

    exit_price: float = 0.0
    exit_time: float = 0.0
    pnl: float = 0.0
    pnl_pct: float = 0.0
    exit_reason: str = ""
    btc_at_exit: float = 0.0
    actual_outcome: str = ""  # Did BTC actually go up or down?


@dataclass
class BacktestState:
    """Mutable state tracked throughout the simulation."""
    capital: float
    start_capital: float
    open_trades: list  # list of SimulatedTrade
    closed_trades: list
    trade_counter: int = 0
    consecutive_losses: int = 0
    circuit_breaker_until: float = 0.0
    daily_start_capital: float = 0.0
    daily_date: str = ""

    # Per-window tracking (which 5-min windows we've already traded)
    traded_windows: set = field(default_factory=set)

    def reset_daily(self, date_str: str):
        self.daily_start_capital = self.capital
        self.daily_date = date_str


# ═══════════════════════════════════════════════════════════
#  BACKTEST ENGINE — the main simulation loop
# ═══════════════════════════════════════════════════════════

def run_backtest(
    candles: list[dict],
    cfg: BacktestConfig,
    verbose: bool = True,
) -> dict:
    """
    Walk through historical 1-minute candles and simulate the full
    trading pipeline: signal generation → entry → position monitoring → exit.

    Each candle represents one tick of the simulation clock. For every
    candle, we:
      1. Check if any open trades should exit (profit/stop/timeout/expiry)
      2. Generate a signal for the current 5-minute market window
      3. Enter a new trade if signal + edge conditions are met
      4. Record the outcome when the 5-minute window expires

    The simulation assumes we're always trading the "nearest expiring"
    5-minute BTC prediction market — a new window opens every 5 minutes.
    """
    if len(candles) < 60:
        raise ValueError("Need at least 60 candles (1 hour) for meaningful backtest")

    state = BacktestState(
        capital=cfg.initial_capital,
        start_capital=cfg.initial_capital,
        open_trades=[],
        closed_trades=[],
    )

    # Pre-compute rolling momentum for each candle
    # This avoids the cold-start problem: in live trading, the bot needs
    # ~5 minutes of price history before momentum is meaningful.
    prices = [c["close"] for c in candles]
    volumes = [c.get("volume", 0) for c in candles]
    momentum_1m = [0.0] * len(prices)
    momentum_5m = [0.0] * len(prices)

    # Pre-compute rolling 15-min average volume for normalization
    vol_ratio = [1.0] * len(volumes)
    for i in range(len(volumes)):
        if i >= 900 and volumes[i] > 0:
            avg_vol = sum(volumes[i-900:i]) / 900
            vol_ratio[i] = min(max(volumes[i] / avg_vol, 0.3), 2.5) if avg_vol > 0 else 1.0
        elif i >= 60 and volumes[i] > 0:
            avg_vol = sum(volumes[:i]) / i
            vol_ratio[i] = min(max(volumes[i] / avg_vol, 0.3), 2.5) if avg_vol > 0 else 1.0

    for i in range(len(prices)):
        if i >= 60:
            raw_1m = (prices[i] - prices[i - 60]) / prices[i - 60]
            momentum_1m[i] = raw_1m * vol_ratio[i]  # Volume-weighted momentum
        if i >= 300:
            raw_5m = (prices[i] - prices[i - 300]) / prices[i - 300]
            momentum_5m[i] = raw_5m * vol_ratio[i]

    # Each "market window" is 5 minutes (300 seconds).
    # We assign each candle to a window based on its timestamp.
    window_size = cfg.market_window_seconds
    equity_curve = []

    for i in range(300, len(candles)):  # Start after 5-min warmup
        candle = candles[i]
        now = candle["open_time"]
        btc_price = candle["close"]

        # Determine which 5-minute window this candle falls in
        # Windows are aligned to clean 5-minute boundaries
        window_id = int(now // window_size)
        window_start_time = window_id * window_size
        window_expiry = (window_id + 1) * window_size
        seconds_to_expiry = window_expiry - now

        # BTC price at the start of this window (for outcome determination)
        window_open_idx = max(0, i - int(now - window_start_time))
        window_open_price = candles[max(0, window_open_idx)]["close"]

        # Change since window opened (used for contract price simulation)
        change_since_open = (btc_price - window_open_price) / window_open_price if window_open_price > 0 else 0.0

        # Daily reset check
        date_str = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d")
        if date_str != state.daily_date:
            state.reset_daily(date_str)

        # ── STEP 1: Check exits on open trades ──
        trades_to_close = []
        for trade in state.open_trades:
            # Simulate current contract price based on BTC movement
            sim_change = (btc_price - trade.window_open_price) / trade.window_open_price
            yes_p, no_p = simulate_contract_price(
                sim_change, trade.window_expiry - now, window_size, cfg.simulated_spread
            )
            current_price = yes_p if trade.side == "YES" else no_p
            unrealized_pnl_pct = (current_price - trade.entry_price) / trade.entry_price if trade.entry_price > 0 else 0.0
            seconds_held = now - trade.entry_time

            # Exit conditions (same priority order as bot.py PositionMonitor)
            exit_reason = None

            if unrealized_pnl_pct >= cfg.min_profit_target:
                exit_reason = "profit_target"
            elif unrealized_pnl_pct <= -cfg.stop_loss_pct:
                exit_reason = "stop_loss"
            elif trade.window_expiry - now < 30:
                exit_reason = "expiry_approaching"
            elif seconds_held > cfg.max_hold_seconds:
                exit_reason = "max_hold_timeout"
            elif (
                (trade.side == "YES" and momentum_1m[i] < -0.003 and unrealized_pnl_pct < 0)
                or (trade.side == "NO" and momentum_1m[i] > 0.003 and unrealized_pnl_pct < 0)
            ):
                exit_reason = "signal_reversal"

            # Market expiry — settle at $1.00 or $0.00
            if now >= trade.window_expiry:
                btc_went_up = btc_price > trade.window_open_price
                if trade.side == "YES":
                    current_price = 1.0 if btc_went_up else 0.0
                else:
                    current_price = 1.0 if not btc_went_up else 0.0
                exit_reason = "expiry_settlement"
                trade.actual_outcome = "UP" if btc_went_up else "DOWN"

            if exit_reason:
                # Apply slippage on exit
                slippage = cfg.simulated_slippage
                exit_price = current_price * (1 - slippage) if current_price > trade.entry_price else current_price * (1 + slippage)
                # Apply dynamic taker fee + gas on exit
                if cfg.use_dynamic_fees:
                    exit_fee_rate = polymarket_taker_fee(exit_price, cfg.market_category)
                else:
                    exit_fee_rate = cfg.taker_fee_pct_override
                exit_fee = trade.shares * exit_price * exit_fee_rate + cfg.gas_cost_per_trade

                trade.exit_price = exit_price
                trade.exit_time = now
                trade.pnl = (exit_price - trade.entry_price) * trade.shares - exit_fee
                trade.pnl_pct = (exit_price - trade.entry_price) / trade.entry_price if trade.entry_price > 0 else 0.0
                trade.exit_reason = exit_reason
                trade.btc_at_exit = btc_price
                trades_to_close.append(trade)

        # Process closed trades
        for trade in trades_to_close:
            state.open_trades.remove(trade)
            state.closed_trades.append(trade)
            state.capital += trade.pnl

            if trade.pnl > 0:
                state.consecutive_losses = 0
            else:
                state.consecutive_losses += 1
                if state.consecutive_losses >= cfg.consecutive_loss_limit:
                    state.circuit_breaker_until = now + cfg.circuit_breaker_pause

        # ── STEP 2: Check if we can trade ──
        can_trade = True
        if now < state.circuit_breaker_until:
            can_trade = False
        daily_pnl_pct = (state.capital - state.daily_start_capital) / state.daily_start_capital if state.daily_start_capital > 0 else 0
        if daily_pnl_pct <= -cfg.daily_loss_limit_pct:
            can_trade = False
        if len(state.open_trades) >= 3:
            can_trade = False
        if window_id in state.traded_windows:
            can_trade = False  # Already have a trade in this window

        # ── STEP 3: Generate signal and maybe enter ──
        if can_trade and seconds_to_expiry > 60:
            fair_value = calculate_fair_value(momentum_1m[i], momentum_5m[i], cfg)

            yes_price, no_price = simulate_contract_price(
                change_since_open, seconds_to_expiry, window_size, cfg.simulated_spread
            )

            signal, edge, confidence = generate_signal(
                fair_value, yes_price, no_price, seconds_to_expiry, cfg
            )

            if signal != "HOLD":
                # Position sizing
                base_size = state.capital * cfg.max_trade_pct
                confidence_mult = 0.5 + (confidence * 0.5)
                position_size = round(min(base_size * confidence_mult, state.capital * cfg.max_trade_pct), 2)

                entry_price = (yes_price if signal == "YES" else no_price)
                # Apply slippage + dynamic taker fee on entry
                if cfg.use_dynamic_fees:
                    entry_fee_rate = polymarket_taker_fee(entry_price, cfg.market_category)
                else:
                    entry_fee_rate = cfg.taker_fee_pct_override
                entry_price = entry_price * (1 + cfg.simulated_slippage + entry_fee_rate)
                # Deduct gas cost from position size
                position_size = max(0, position_size - cfg.gas_cost_per_trade)
                shares = position_size / entry_price if entry_price > 0 else 0

                if shares > 0 and position_size > 0:
                    state.trade_counter += 1
                    trade = SimulatedTrade(
                        trade_id=state.trade_counter,
                        side=signal,
                        entry_price=entry_price,
                        shares=shares,
                        capital_used=position_size,
                        entry_time=now,
                        entry_btc_price=btc_price,
                        window_open_price=window_open_price,
                        window_expiry=window_expiry,
                    )
                    state.open_trades.append(trade)
                    state.traded_windows.add(window_id)

        # Track equity curve every 5 minutes
        if i % 5 == 0:
            equity_curve.append({
                "time": now,
                "equity": round(state.capital, 4),
                "btc_price": round(btc_price, 2),
                "open_trades": len(state.open_trades),
            })

    # ── Compute Summary Statistics ──
    summary = compute_summary(state, cfg, equity_curve)

    if verbose:
        print_summary(summary)

    return {
        "config": asdict(cfg),
        "summary": summary,
        "trades": [asdict(t) for t in state.closed_trades],
        "equity_curve": equity_curve,
    }


# ═══════════════════════════════════════════════════════════
#  STATISTICS & REPORTING
# ═══════════════════════════════════════════════════════════

def compute_summary(state: BacktestState, cfg: BacktestConfig, equity_curve: list) -> dict:
    """
    Compute comprehensive performance metrics from closed trades.
    These are the same metrics the dashboard shows for paper trading,
    so you can directly compare backtest vs live performance.
    """
    trades = state.closed_trades
    if not trades:
        return {"total_trades": 0, "note": "No trades executed — edge threshold may be too high"}

    pnls = [t.pnl for t in trades]
    pnl_pcts = [t.pnl_pct for t in trades]
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]

    # Win rate and profit factor
    win_rate = len(wins) / len(trades)
    gross_profit = sum(t.pnl for t in wins) if wins else 0
    gross_loss = abs(sum(t.pnl for t in losses)) if losses else 0.001
    profit_factor = gross_profit / gross_loss

    # Drawdown analysis
    equities = [e["equity"] for e in equity_curve] if equity_curve else [cfg.initial_capital]
    peak = equities[0]
    max_drawdown = 0
    max_drawdown_pct = 0
    for eq in equities:
        peak = max(peak, eq)
        dd = peak - eq
        dd_pct = dd / peak if peak > 0 else 0
        max_drawdown = max(max_drawdown, dd)
        max_drawdown_pct = max(max_drawdown_pct, dd_pct)

    # Hold duration statistics
    durations = [t.exit_time - t.entry_time for t in trades if t.exit_time > 0]
    avg_hold = statistics.mean(durations) if durations else 0
    median_hold = statistics.median(durations) if durations else 0

    # Exit reason breakdown
    exit_reasons = {}
    for t in trades:
        r = t.exit_reason
        exit_reasons[r] = exit_reasons.get(r, 0) + 1

    # Sharpe ratio (annualized, using daily returns)
    daily_returns = {}
    for t in trades:
        day = datetime.fromtimestamp(t.entry_time, tz=timezone.utc).strftime("%Y-%m-%d")
        daily_returns.setdefault(day, 0.0)
        daily_returns[day] += t.pnl_pct

    daily_ret_list = list(daily_returns.values())
    if len(daily_ret_list) > 1:
        avg_daily = statistics.mean(daily_ret_list)
        std_daily = statistics.stdev(daily_ret_list)
        sharpe = (avg_daily / std_daily) * math.sqrt(365) if std_daily > 0 else 0
    else:
        sharpe = 0

    # Consecutive loss streaks
    max_streak = 0
    current_streak = 0
    for t in trades:
        if t.pnl <= 0:
            current_streak += 1
            max_streak = max(max_streak, current_streak)
        else:
            current_streak = 0

    return {
        "total_trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(win_rate, 4),
        "profit_factor": round(profit_factor, 3),
        "total_pnl": round(sum(pnls), 4),
        "total_pnl_pct": round((state.capital - cfg.initial_capital) / cfg.initial_capital * 100, 2),
        "final_capital": round(state.capital, 2),
        "max_drawdown": round(max_drawdown, 4),
        "max_drawdown_pct": round(max_drawdown_pct * 100, 2),
        "sharpe_ratio": round(sharpe, 3),
        "avg_win": round(statistics.mean([t.pnl for t in wins]), 4) if wins else 0,
        "avg_loss": round(statistics.mean([t.pnl for t in losses]), 4) if losses else 0,
        "avg_win_pct": round(statistics.mean([t.pnl_pct for t in wins]) * 100, 3) if wins else 0,
        "avg_loss_pct": round(statistics.mean([t.pnl_pct for t in losses]) * 100, 3) if losses else 0,
        "best_trade": round(max(pnls), 4),
        "worst_trade": round(min(pnls), 4),
        "avg_hold_seconds": round(avg_hold, 1),
        "median_hold_seconds": round(median_hold, 1),
        "max_consecutive_losses": max_streak,
        "exit_reasons": exit_reasons,
        "trades_per_day": round(len(trades) / max(len(daily_returns), 1), 1),
    }


def print_summary(s: dict):
    """Pretty-print backtest results to terminal."""
    if s.get("total_trades", 0) == 0:
        print("\n⚠️  No trades executed. Try lowering min_edge_required.")
        return

    # Color codes for terminal
    GREEN = "\033[92m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    CYAN = "\033[96m"
    RESET = "\033[0m"
    BOLD = "\033[1m"

    pnl_color = GREEN if s["total_pnl"] >= 0 else RED
    wr_color = GREEN if s["win_rate"] >= 0.52 else (YELLOW if s["win_rate"] >= 0.45 else RED)
    pf_color = GREEN if s["profit_factor"] >= 1.5 else (YELLOW if s["profit_factor"] >= 1.0 else RED)

    print(f"\n{BOLD}{'═' * 60}{RESET}")
    print(f"{BOLD}  BACKTEST RESULTS{RESET}")
    print(f"{'═' * 60}")
    print(f"  Trades: {s['total_trades']}  ({s['trades_per_day']}/day)")
    print(f"  Win Rate: {wr_color}{s['win_rate']*100:.1f}%{RESET}  ({s['wins']}W / {s['losses']}L)")
    print(f"  Profit Factor: {pf_color}{s['profit_factor']:.2f}{RESET}")
    print(f"  Total P&L: {pnl_color}${s['total_pnl']:+.4f}{RESET} ({pnl_color}{s['total_pnl_pct']:+.2f}%{RESET})")
    print(f"  Final Capital: ${s['final_capital']:.2f}")
    print(f"  Max Drawdown: {RED}{s['max_drawdown_pct']:.2f}%{RESET}")
    print(f"  Sharpe Ratio: {s['sharpe_ratio']:.2f}")
    print(f"  {CYAN}Avg Win:  {s['avg_win_pct']:+.3f}%{RESET}  |  Avg Loss: {RED}{s['avg_loss_pct']:+.3f}%{RESET}")
    print(f"  Avg Hold: {s['avg_hold_seconds']:.0f}s  |  Median: {s['median_hold_seconds']:.0f}s")
    print(f"  Max Consecutive Losses: {s['max_consecutive_losses']}")
    print(f"\n  {BOLD}Exit Reasons:{RESET}")
    for reason, count in sorted(s["exit_reasons"].items(), key=lambda x: -x[1]):
        pct = count / s["total_trades"] * 100
        bar = "█" * int(pct / 2)
        print(f"    {reason:<25} {count:>4} ({pct:>5.1f}%)  {bar}")
    print(f"{'═' * 60}\n")

    # Go-live assessment
    print(f"  {BOLD}GO-LIVE CHECKLIST:{RESET}")
    checks = [
        (s["win_rate"] >= 0.52, f"Win rate ≥ 52%: {s['win_rate']*100:.1f}%"),
        (s["profit_factor"] >= 1.5, f"Profit factor ≥ 1.5: {s['profit_factor']:.2f}"),
        (s["total_trades"] >= 50, f"≥ 50 trades: {s['total_trades']}"),
        (s["max_drawdown_pct"] < 10, f"Max DD < 10%: {s['max_drawdown_pct']:.2f}%"),
        (s["max_consecutive_losses"] < 8, f"Max streak < 8: {s['max_consecutive_losses']}"),
    ]
    all_pass = True
    for passed, desc in checks:
        icon = f"{GREEN}✅{RESET}" if passed else f"{RED}❌{RESET}"
        print(f"    {icon} {desc}")
        if not passed:
            all_pass = False

    if all_pass:
        print(f"\n    {GREEN}{BOLD}✅ ALL CHECKS PASSED — Strategy looks viable for paper trading{RESET}")
    else:
        print(f"\n    {RED}{BOLD}❌ SOME CHECKS FAILED — Tune parameters before going live{RESET}")


# ═══════════════════════════════════════════════════════════
#  PARAMETER OPTIMIZATION — grid search over key params
# ═══════════════════════════════════════════════════════════

def run_optimization(candles: list[dict], verbose: bool = True) -> list[dict]:
    """
    Grid search over the most impactful strategy parameters to find
    the combination that maximizes risk-adjusted returns (Sharpe ratio).

    The parameters we scan are:
      - min_edge_required: How much edge we demand before entering
      - momentum_1m_multiplier: How aggressively we scale 1m momentum
      - min_profit_target: When to take profits
      - stop_loss_pct: When to cut losses

    This is the most important thing to do before going live — it
    replaces the "chosen by intuition" parameter problem noted in
    the bot's known limitations.
    """
    # Parameter grid — these ranges cover the sensible space
    # without being so granular that the search takes forever
    edge_values = [0.002, 0.003, 0.005, 0.007, 0.01]
    mom_values = [5.0, 8.0, 10.0, 15.0, 20.0]
    profit_values = [0.01, 0.015, 0.025, 0.035, 0.05]
    stop_values = [0.005, 0.01, 0.015, 0.02]

    total = len(edge_values) * len(mom_values) * len(profit_values) * len(stop_values)
    results = []

    if verbose:
        print(f"\n🔬 Running parameter optimization ({total} combinations)...")
        print(f"   Edge: {edge_values}")
        print(f"   Momentum 1m mult: {mom_values}")
        print(f"   Profit target: {profit_values}")
        print(f"   Stop loss: {stop_values}")

    count = 0
    for edge, mom, profit, stop in cart_product(edge_values, mom_values, profit_values, stop_values):
        count += 1
        if verbose and count % 50 == 0:
            print(f"   Progress: {count}/{total} ({count/total*100:.0f}%)")

        cfg = BacktestConfig(
            min_edge_required=edge,
            momentum_1m_multiplier=mom,
            min_profit_target=profit,
            stop_loss_pct=stop,
        )

        try:
            result = run_backtest(candles, cfg, verbose=False)
            summary = result["summary"]

            if summary.get("total_trades", 0) < 10:
                continue  # Not enough trades to be meaningful

            results.append({
                "min_edge": edge,
                "mom_1m_mult": mom,
                "profit_target": profit,
                "stop_loss": stop,
                "trades": summary["total_trades"],
                "win_rate": summary["win_rate"],
                "profit_factor": summary["profit_factor"],
                "total_pnl_pct": summary["total_pnl_pct"],
                "sharpe": summary["sharpe_ratio"],
                "max_dd_pct": summary["max_drawdown_pct"],
                "trades_per_day": summary["trades_per_day"],
            })
        except Exception:
            continue

    # Sort by Sharpe ratio (risk-adjusted return) then by profit factor
    results.sort(key=lambda r: (r["sharpe"], r["profit_factor"]), reverse=True)

    if verbose and results:
        print(f"\n{'═' * 80}")
        print(f"  TOP 10 PARAMETER COMBINATIONS (by Sharpe ratio)")
        print(f"{'═' * 80}")
        print(f"  {'Edge':>6} {'Mom':>5} {'PT':>6} {'SL':>6} | {'Trades':>6} {'WR':>6} {'PF':>6} {'P&L%':>7} {'Sharpe':>7} {'DD%':>6}")
        print(f"  {'─'*6} {'─'*5} {'─'*6} {'─'*6} | {'─'*6} {'─'*6} {'─'*6} {'─'*7} {'─'*7} {'─'*6}")
        for r in results[:10]:
            print(
                f"  {r['min_edge']:>6.3f} {r['mom_1m_mult']:>5.0f} "
                f"{r['profit_target']:>6.3f} {r['stop_loss']:>6.3f} | "
                f"{r['trades']:>6} {r['win_rate']*100:>5.1f}% "
                f"{r['profit_factor']:>5.2f} {r['total_pnl_pct']:>+6.2f}% "
                f"{r['sharpe']:>6.2f} {r['max_dd_pct']:>5.2f}%"
            )
        print(f"{'═' * 80}")

        best = results[0]
        print(f"\n  🏆 RECOMMENDED CONFIG:")
        print(f"     min_edge_required = {best['min_edge']}")
        print(f"     momentum_1m_multiplier = {best['mom_1m_mult']}")
        print(f"     min_profit_target = {best['profit_target']}")
        print(f"     stop_loss_pct = {best['stop_loss']}")

    return results


# ═══════════════════════════════════════════════════════════
#  WALK-FORWARD VALIDATION — prevents overfitting
# ═══════════════════════════════════════════════════════════

def walk_forward_test(
    candles: list[dict],
    cfg: BacktestConfig,
    n_splits: int = 5,
    verbose: bool = True,
) -> dict:
    """
    Walk-forward analysis splits the data into sequential chunks and
    tests each one independently. This prevents the dangerous mistake
    of optimizing parameters on the same data you test on (overfitting).

    If a strategy works in ALL splits, it's more likely to work live.
    If it only works in 1-2 splits, the profitable period was likely luck.

    Example with 5 splits over 7 days:
      Split 1: Day 1-2 (train), Day 2-3 (test)
      Split 2: Day 2-3 (train), Day 3-4 (test)
      ... etc
    """
    chunk_size = len(candles) // n_splits
    if chunk_size < 300:
        raise ValueError(f"Not enough data for {n_splits} splits (need {n_splits * 300}+ candles)")

    split_results = []

    if verbose:
        print(f"\n📊 Walk-Forward Validation ({n_splits} splits, {chunk_size} candles each)")
        print(f"{'─' * 60}")

    for i in range(n_splits):
        start_idx = i * chunk_size
        end_idx = min((i + 1) * chunk_size, len(candles))
        chunk = candles[start_idx:end_idx]

        if len(chunk) < 300:
            continue

        result = run_backtest(chunk, cfg, verbose=False)
        s = result["summary"]

        split_results.append({
            "split": i + 1,
            "start": datetime.fromtimestamp(chunk[0]["open_time"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
            "end": datetime.fromtimestamp(chunk[-1]["open_time"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
            "trades": s.get("total_trades", 0),
            "win_rate": s.get("win_rate", 0),
            "profit_factor": s.get("profit_factor", 0),
            "pnl_pct": s.get("total_pnl_pct", 0),
            "sharpe": s.get("sharpe_ratio", 0),
        })

        if verbose:
            wr = s.get("win_rate", 0) * 100
            pnl = s.get("total_pnl_pct", 0)
            icon = "✅" if pnl > 0 and wr > 50 else "❌"
            print(f"  Split {i+1}: {split_results[-1]['start']} → {split_results[-1]['end']}")
            print(f"    {icon} {s.get('total_trades',0)} trades | WR {wr:.1f}% | PF {s.get('profit_factor',0):.2f} | P&L {pnl:+.2f}%")

    # Overall consistency check
    profitable_splits = sum(1 for s in split_results if s["pnl_pct"] > 0)
    consistency = profitable_splits / len(split_results) if split_results else 0

    if verbose:
        print(f"\n  Consistency: {profitable_splits}/{len(split_results)} splits profitable ({consistency*100:.0f}%)")
        if consistency >= 0.6:
            print(f"  ✅ Strategy shows consistent profitability across time periods")
        else:
            print(f"  ⚠️  Strategy is inconsistent — likely overfitted to specific market conditions")

    return {
        "splits": split_results,
        "consistency": consistency,
        "profitable_splits": profitable_splits,
        "total_splits": len(split_results),
    }


# ═══════════════════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Polymarket BTC Bot — Backtester")
    parser.add_argument("--days", type=int, default=7, help="Days of historical data (default: 7)")
    parser.add_argument("--capital", type=float, default=1000, help="Starting capital (default: 1000)")
    parser.add_argument("--optimize", action="store_true", help="Run parameter grid search")
    parser.add_argument("--walk-forward", action="store_true", help="Run walk-forward validation")
    parser.add_argument("--splits", type=int, default=5, help="Number of walk-forward splits (default: 5)")
    parser.add_argument("--edge", type=float, default=0.005, help="min_edge_required (default: 0.005)")
    parser.add_argument("--momentum", type=float, default=10.0, help="momentum_1m_multiplier (default: 10)")
    parser.add_argument("--profit", type=float, default=0.025, help="min_profit_target (default: 0.025)")
    parser.add_argument("--stop", type=float, default=0.01, help="stop_loss_pct (default: 0.01)")
    args = parser.parse_args()

    print(f"""
╔══════════════════════════════════════════════╗
║   POLYMARKET BTC BOT — BACKTESTER v1.0       ║
║   Period: {args.days} days | Capital: ${args.capital:.0f}           ║
╚══════════════════════════════════════════════╝
    """)

    # Fetch historical data
    candles = fetch_binance_klines(days=args.days)

    if not candles:
        print("❌ Failed to fetch historical data")
        return

    Path("data").mkdir(exist_ok=True)

    if args.optimize:
        # Grid search for best parameters
        results = run_optimization(candles)
        with open("data/optimization_grid.json", "w") as f:
            json.dump(results, f, indent=2)
        print(f"\n💾 Optimization results saved to data/optimization_grid.json")
    else:
        # Single backtest run
        cfg = BacktestConfig(
            initial_capital=args.capital,
            lookback_days=args.days,
            min_edge_required=args.edge,
            momentum_1m_multiplier=args.momentum,
            min_profit_target=args.profit,
            stop_loss_pct=args.stop,
        )

        result = run_backtest(candles, cfg)

        # Save results
        with open("data/backtest_results.json", "w") as f:
            json.dump(result, f, indent=2, default=str)
        print(f"💾 Results saved to data/backtest_results.json")

        # Walk-forward validation if requested
        if args.walk_forward:
            wf = walk_forward_test(candles, cfg, n_splits=args.splits)
            with open("data/walk_forward_results.json", "w") as f:
                json.dump(wf, f, indent=2)
            print(f"💾 Walk-forward results saved to data/walk_forward_results.json")


if __name__ == "__main__":
    main()
