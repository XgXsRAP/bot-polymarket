# Polymarket BTC Bot — Deep Analysis & Improvement Roadmap

## Executive Summary

Your bot has strong engineering foundations — the async architecture, multi-source price feed fallback chain (Binance → Kraken → REST polling), and the modular component design are all well-executed. The signal pipeline (BTC momentum → fair value → edge detection → risk-filtered entry) is the right conceptual approach for this market microstructure.

However, several critical gaps need addressing before the strategy can be expected to produce consistent profits. This analysis covers the architecture, signal quality, speed, risk management, and provides concrete code and strategies to close those gaps.

---

## 1. Signal Engine — The Core Problem

### What It Does Well

The `SignalEngine.calculate_fair_value()` function combines multiple signal sources (Chainlink deviation, 1m/5m momentum, CVD, liquidation pressure, funding, OI) with explicit weight budgets. The Chainlink deviation signal is particularly clever — since Polymarket settles on Chainlink's price, trading the gap between Binance (fast) and Chainlink (slow, updates on 0.5% deviation threshold) gives you a genuine informational edge.

### What Needs Fixing

**Problem 1: Linear momentum scaling is dangerously naive.** The formula `change_1m * 10` maps a 0.1% BTC move to a 100-basis-point fair value shift. But the relationship between momentum and future price direction is NOT linear. A 0.01% move is noise. A 0.5% move in 1 minute is a strong signal. The current linear scaling treats them proportionally when it should treat them very differently.

**Fix:** Replace the linear multiplier with a sigmoid or tanh transformation that compresses small moves (noise) and amplifies large moves (real signal):

```python
# BEFORE (linear — bad)
adj_1m = min(max(btc.change_1m * 10, -0.15), 0.15)

# AFTER (sigmoid — much better)
import math
def momentum_to_signal(change, sensitivity=200, cap=0.15):
    """
    Tanh maps small moves → ~0 (noise rejection)
    and large moves → ±cap (strong conviction).
    sensitivity=200 means a 0.5% move maps to ~99% of cap.
    """
    return math.tanh(change * sensitivity) * cap

adj_1m = momentum_to_signal(btc.change_1m, sensitivity=200, cap=0.15)
adj_5m = momentum_to_signal(btc.change_5m, sensitivity=100, cap=0.10)
```

**Problem 2: No volume normalization.** A 0.1% move on 10x average volume is far more meaningful than the same move on low volume. The signal engine ignores this entirely, which means it enters trades on low-conviction moves just as aggressively as high-conviction ones.

**Fix:** Weight the momentum signal by relative volume:

```python
# Track rolling 15-min average volume
volume_ratio = current_volume / rolling_avg_volume_15m
# Amplify signal on high volume, dampen on low volume
volume_multiplier = min(max(volume_ratio, 0.3), 2.5)
adj_1m = momentum_to_signal(btc.change_1m * volume_multiplier, 200, 0.15)
```

**Problem 3: The simulated contract prices don't reflect real market microstructure.** In paper trading, the bot uses Gamma API prices which update every ~30 seconds. But real Polymarket contract prices move continuously on the CLOB. The paper trader simulates fills at mid-price, which is optimistic — in reality, you'd pay the ask (buying) and receive the bid (selling), and both are worse than mid.

**Fix:** Always use CLOB orderbook bid/ask for entry/exit simulation. The dashboard.py already fetches these — pipe them into the paper trader:

```python
# Use ask price for buys, bid price for sells (pessimistic fill)
entry_price = contract.yes_ask if signal == Signal.YES else contract.no_ask
exit_price = contract.yes_bid if exiting YES else contract.no_bid
```

---

## 2. Market Making Strategy — A Better Approach

Your current strategy is directional (predicting UP or DOWN). A market making strategy is fundamentally different and potentially more profitable for this market structure.

### How Market Making Works on Polymarket

Instead of predicting direction, you simultaneously quote both sides:

```
Your bid on YES at 0.48  |  Your ask on YES at 0.53
Your bid on NO  at 0.48  |  Your ask on NO  at 0.53

If someone buys your YES ask at 0.53 and someone else buys your NO ask at 0.53:
  Revenue = 0.53 + 0.53 = 1.06
  Market settles at YES=$1 + NO=$0 OR YES=$0 + NO=$1 = $1.00
  Profit = 1.06 - 1.00 = $0.06 (6 cents per contract pair)
```

The key insight is that YES + NO always settle to $1.00 on Polymarket. If you can sell both sides for a combined price > $1.00, you profit regardless of the outcome.

### Strategy: Delta-Neutral Market Making

```python
class MarketMaker:
    """
    Quotes both sides of BTC prediction markets.
    Profit comes from the spread, not from predicting direction.

    Core logic:
    1. Estimate fair value (same as current signal engine)
    2. Place bid below fair value, ask above fair value
    3. When both sides fill, lock in the spread as profit
    4. Use BTC momentum to skew quotes (shift risk away from danger)

    Advantages over directional trading:
    - Profitable in sideways AND trending markets
    - Lower variance (many small wins vs occasional big wins/losses)
    - Natural inventory management (long YES = short NO)
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self.half_spread = 0.025      # 2.5 cents per side
        self.skew_factor = 0.5        # How much momentum skews the quotes
        self.max_inventory = 50       # Max shares on one side before hedging
        self.inventory_yes = 0        # Current YES share inventory
        self.inventory_no = 0         # Current NO share inventory

    def compute_quotes(self, fair_value, btc_momentum):
        """
        Generate bid/ask quotes for YES and NO sides.

        The skew shifts quotes away from the side where we have excess
        inventory or where momentum suggests risk. If BTC is trending
        up strongly, we skew YES asks higher (less eager to sell YES
        because it's more likely to settle at $1.00).
        """
        # Momentum skew: shift mid-price in the direction of momentum
        # This provides natural hedge against adverse selection
        skew = btc_momentum * self.skew_factor

        # Inventory skew: if we're long YES, make YES asks cheaper
        # to encourage sells and reduce inventory
        inv_imbalance = (self.inventory_yes - self.inventory_no) / max(self.max_inventory, 1)
        inv_skew = -inv_imbalance * 0.01  # 1 cent per unit of imbalance

        adjusted_mid = fair_value + skew + inv_skew

        return {
            "yes_bid": max(0.02, adjusted_mid - self.half_spread),
            "yes_ask": min(0.98, adjusted_mid + self.half_spread),
            "no_bid": max(0.02, (1 - adjusted_mid) - self.half_spread),
            "no_ask": min(0.98, (1 - adjusted_mid) + self.half_spread),
        }
```

### When to Use Market Making vs Directional

Market making works best when the market has sufficient volume (both sides get filled regularly) and when the spread is wide enough to cover your costs. Directional trading works best when you have a clear edge — the Chainlink deviation signal, for instance, is a directional edge.

The optimal approach combines both: use market making as the base strategy (steady income from spread capture) and overlay directional bets when the Chainlink deviation signal is strong (extra profit from predicting direction).

---

## 3. Backtesting Framework — Closing the Biggest Gap

The `backtest.py` file I've built addresses the #1 issue in the Known Limitations: "Strategy parameters were chosen by intuition — no historical validation."

### How to Use It

```bash
# Basic backtest with default parameters (7 days)
python backtest.py

# Custom period and capital
python backtest.py --days 14 --capital 500

# Parameter optimization — finds the best config
python backtest.py --optimize

# Walk-forward validation — tests if the strategy is overfitted
python backtest.py --walk-forward --splits 5

# Test specific parameters
python backtest.py --edge 0.007 --momentum 15 --profit 0.03 --stop 0.015
```

### What the Backtester Simulates

The backtester walks through historical 1-minute BTC candles and simulates the full trading pipeline for each minute. It generates synthetic Polymarket contract prices based on BTC momentum (since Polymarket doesn't provide historical contract-level data), applies realistic slippage and spread costs, and tracks every trade through entry → hold → exit just like the live bot.

The critical addition is **walk-forward validation**. This splits your data into sequential chunks and tests each one independently. If a strategy only profits in 1-2 out of 5 time periods, the profitable period was likely luck. If it profits in 4-5 out of 5, the edge is more likely real.

### Limitations to Be Honest About

The simulated contract prices are an approximation. Real Polymarket orderbooks have varying depth, liquidity gaps, and participant behavior that synthetic data can't capture. The backtester tells you "this strategy COULD work" — not "this strategy WILL work." Paper trading with real market data (which the bot already does) is the essential second validation step before going live.

---

## 4. BTC Price Feed Speed Improvements

### Current Architecture Assessment

The feed fallback chain (Binance WS → Kraken WS → Kraken REST) is well-designed. The 100ms main loop target is reasonable. But several improvements can reduce latency by 30-50ms:

### Improvement 1: Use Binary WebSocket Frames

Binance supports both JSON and binary (protobuf-like) message formats. The binary format is ~40% smaller and parses faster:

```python
# In BTCPriceFeed._connect_binance():
# Use the "mini ticker" stream instead of the full ticker
# It sends only: event_time, symbol, close, open, high, low, volume, quote_volume
binance_ws_url = "wss://stream.binance.com:9443/ws/btcusdt@miniTicker"

# The mini ticker message is ~60% smaller than the full ticker
# Parsing 100 bytes vs 250 bytes saves ~5ms per message
```

### Improvement 2: Parallel Price Source Aggregation

Instead of falling back sequentially (Binance → Kraken → REST), run all three simultaneously and use the freshest price:

```python
class MultiSourcePriceFeed:
    """
    Run Binance, Kraken, and Hyperliquid in parallel.
    Use the most recently updated price for signal generation.
    Advantages:
    - No fallback delay (already have all prices)
    - Cross-validation (if sources diverge > 0.1%, something is wrong)
    - Lower effective latency (use whichever updates first)
    """

    def __init__(self):
        self.sources = {}  # {"binance": (price, timestamp), ...}

    @property
    def best_price(self):
        """Return the most recently updated price across all sources."""
        if not self.sources:
            return None
        freshest = max(self.sources.items(), key=lambda x: x[1][1])
        return freshest[1][0]  # price from freshest source

    @property
    def price_divergence(self):
        """Check if sources agree. Large divergence = something broken."""
        prices = [p for p, _ in self.sources.values() if p > 0]
        if len(prices) < 2:
            return 0
        return (max(prices) - min(prices)) / min(prices)
```

### Improvement 3: Reduce Polymarket Poll Interval

The current 30-second Gamma API poll is too slow for a bot targeting 100ms latency. The CLOB WebSocket provides real-time orderbook updates, but you're currently only subscribing after market discovery. Subscribe proactively to BTC market condition IDs as soon as they appear:

```python
# In PolymarketFeed._ws_prices():
# Instead of subscribing once, re-subscribe whenever new markets are discovered
# Use a set to track subscriptions and avoid duplicates
self._subscribed_cids = set()

async def _ensure_subscriptions(self, ws):
    for cid in self.markets:
        if cid not in self._subscribed_cids:
            await ws.send(json.dumps({
                "type": "subscribe", "channel": "book", "market": cid
            }))
            self._subscribed_cids.add(cid)
```

### Improvement 4: Pre-compute Everything

The signal engine does several small calculations every 100ms loop iteration. Pre-compute momentum, fair value, and signal for each candle tick and cache the result. Only recompute when BTC price actually changes:

```python
class CachedSignalEngine:
    def __init__(self):
        self._last_btc_price = 0
        self._cached_fair_value = 0.5
        self._cache_valid = False

    def get_fair_value(self, btc):
        # Only recompute if price actually changed
        if btc.price == self._last_btc_price and self._cache_valid:
            return self._cached_fair_value
        self._cached_fair_value = self._compute(btc)
        self._last_btc_price = btc.price
        self._cache_valid = True
        return self._cached_fair_value
```

---

## 5. Risk Management Improvements

### What's Good

The risk system has the right components: daily loss limits, consecutive loss circuit breaker, position sizing by confidence, and maximum concurrent positions. The new rolling win-rate circuit breaker (pause if WR < 30% over last 20 trades) is a smart addition.

### What's Missing

**Problem 1: No correlation-aware position limits.** If you hold 3 positions and all 3 are YES (bullish BTC), a single BTC dump wipes all three. The maximum concurrent position limit should consider DIRECTIONAL exposure, not just count:

```python
def can_add_position(self, signal, open_trades):
    # Count directional exposure
    yes_count = sum(1 for t in open_trades if t.side == "YES")
    no_count = sum(1 for t in open_trades if t.side == "NO")

    # Max 2 positions in same direction
    if signal == Signal.YES and yes_count >= 2:
        return False, "Max directional exposure (2 YES)"
    if signal == Signal.NO and no_count >= 2:
        return False, "Max directional exposure (2 NO)"
    return True, "OK"
```

**Problem 2: No volatility-adjusted position sizing.** When BTC is moving ±1% per minute (high vol), a $5 position can hit the stop loss in seconds. When BTC is moving ±0.01% per minute (low vol), the same $5 position barely moves. Position size should scale inversely with recent volatility:

```python
def volatility_adjusted_size(self, base_size, btc_volatility_1h):
    """
    Scale position size inversely with volatility.
    High vol → smaller positions (prevent large losses)
    Low vol → larger positions (capture more of the small edge)
    """
    # Baseline: 0.5% hourly vol is "normal" for BTC
    vol_ratio = 0.005 / max(btc_volatility_1h, 0.001)
    vol_multiplier = min(max(vol_ratio, 0.3), 2.0)
    return base_size * vol_multiplier
```

**Problem 3: No drawdown-based throttling.** The daily loss limit is binary (trading or stopped). A better approach throttles gradually — reduce position sizes as drawdown increases:

```python
def drawdown_multiplier(self):
    """
    Gradually reduce size as daily drawdown increases.
    0% DD → 1.0× (full size)
    5% DD → 0.5× (half size)
    10% DD → 0.0× (stopped)
    """
    dd = abs(min(self.daily_pnl_pct, 0))
    limit = self.cfg.daily_loss_limit_pct
    return max(0, 1.0 - (dd / limit))
```

---

## 6. Dashboard Improvements

The new React dashboard (`polybot_dashboard.jsx`) replaces the terminal-based Rich dashboard with a professional trading terminal interface. Key improvements include multi-panel layout with BTC price, capital, win rate, and position count all visible at once; signal component gauges showing CVD, liquidation pressure, funding, and OI signals as visual gauges instead of raw numbers; a cumulative P&L chart rendered as an inline SVG sparkline; tabbed navigation between active markets, open positions, and risk metrics; and visual risk meters for daily loss usage, consecutive losses, and position slots.

The dashboard uses simulated data in demo mode (for development) but the component structure matches exactly what `bot_state.json` provides, so connecting it to the live bot requires only replacing the mock data generator with a fetch to the state file.

---

## 7. Strategy Recommendations — Priority Order

**Immediate (do before any more paper trading):**

1. Run the backtester with `--optimize` to find parameter values backed by data instead of intuition
2. Replace linear momentum scaling with tanh/sigmoid transformation
3. Add volume normalization to the signal engine
4. Use CLOB bid/ask prices for paper trade fills instead of mid-price

**Short-term (before going live):**

5. Add volatility-adjusted position sizing
6. Add directional exposure limits (max 2 same-side positions)
7. Implement parallel price feed (all sources simultaneously)
8. Add the market making strategy as a second mode alongside directional

**Medium-term (after first week of live trading):**

9. Persist open trades to disk (crash recovery)
10. Add Telegram/Discord alerts on circuit breaker events
11. Build a proper orderbook depth filter (reject markets where spread > 2 cents)
12. Implement trailing stops for winning trades in hold-to-expiry mode

---

## 8. Key Questions for You

Before implementing these changes, a few things would help me prioritize:

**On market availability:** How often do you see active BTC 5-minute markets on Polymarket? The bot logs show "No active BTC market found" frequently. If markets are sparse, the market making strategy becomes more important because it can also profit during low-activity periods by providing liquidity.

**On capital size:** The current $100 capital with $5 position sizes means each trade is only 5% of capital. At 2.5% profit target, that's $0.125 per winning trade. Transaction costs (gas on Polygon + Polymarket fees) could eat a significant portion of this. What's your target capital once you go live?

**On the Hyperliquid API:** The `hyperliquid_api.py` import is optional and currently missing. The enhanced signals (CVD, liquidations, funding) are potentially valuable but only if the data is fresh and reliable. Is this module something you've built, or should I build it as part of this project?

**On latency requirements:** The bot targets 100ms loop time, but real HFT on Polymarket operates at 10-20ms. If other bots are faster, they'll capture the Chainlink deviation edge before you. Is co-location (running near Polymarket's infrastructure) something you're considering?
