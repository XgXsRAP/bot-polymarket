# Market Maker Bot — Full Structure Analysis & TODO List

**Path:** `C:\Users\Admin\OneDrive\Documentos\claude-bot\market_maker`  
**Date:** April 5, 2026

---

## 1. Current File Structure Map

```
market_maker/
├── mm_enhanced_1.py          ← MAIN ENTRY POINT (paper, live, signals, resolver, backtest)
├── polymarket_gamma.py       ← Polymarket market discovery + YES/NO price feed
├── hyperliquid_api.py        ← Derivatives side-data (CVD, funding, OI, liq)
├── binance_feed.py           ← BTC price via Binance WebSocket (imported as BinanceBTCFeed)
├── chainlink_feed.py         ← Settlement price feed (Polygon RPC)
├── paper_trader.py           ← Simulated fill engine for paper mode
├── live_order_manager.py     ← Real CLOB order placement (py-clob-client)
├── confidence.py             ← Confidence scoring / tier system
├── fees.py                   ← Polymarket dynamic fee model
├── alerting.py               ← Telegram/Discord notifications
├── mm_dashboard.py           ← Terminal + web dashboard (aiohttp)
├── mm_guide.md               ← Guide doc (OUTDATED — needs full rewrite)
├── data/
│   ├── paper_mm_state.json   ← Persisted paper state
│   ├── paper_mm_fills.json   ← Fill history
│   └── mm_historical.jsonl   ← Recorded ticks for backtesting
└── backtest/
    ├── BOT_ANALYSIS.md       ← Previous analysis (still relevant)
    ├── backtest_v1failed.py  ← Directional backtester (failed approach)
    ├── backtest_impV1.py     ← Improved MM backtester
    ├── backtest_unified.py   ← Unified backtester with grid search
    └── polybot_dashboard.jsx ← React dashboard (for directional bot)
```

---

## 2. ROOT CAUSE: Why YES/NO Prices Get Stuck

**This is the #1 bug you're experiencing.** The bot detects one price and then keeps logging the same value. Here's exactly why:

### Bug Chain (3 interacting failures)

**Failure 1 — CLOB WebSocket silently dies, REST fallback is too slow.**

In `polymarket_gamma.py`, the WS book listener connects to `wss://clob.polymarket.com/ws/` and subscribes to the `book` channel. But there are two critical problems:

```
_ws_book_listener() → connects → subscribes to condition_id
  BUT:
  1. If HTTP 404/401/403 → it RETURNS (exits forever), no retry
  2. price_change events with size==0 are SKIPPED ("REST will correct within 10s")
  3. The REST correction loop (_clob_book_loop) only runs every 3 seconds
     AND it uses the Gamma REST API, not the CLOB orderbook API
```

When the WS connection gets an auth error (very common — the CLOB book channel often requires credentials), the listener **silently exits permanently** with just an info log. From that point, prices are only updated by:
- `_clob_book_loop()` — polls Gamma REST every 3s, but only gets `bestBid`/`bestAsk` from the Gamma API response, which itself can be stale (Gamma caches aggressively)
- `_poll_loop()` → `_fetch_current()` — runs every 10s, fetches market data including `outcomePrices`

**Failure 2 — `_handle_book_msg()` only updates prices UPWARD for bids and DOWNWARD for asks.**

```python
# In price_change handler:
if side == "buy" and price > self._best_bid:     # ← only raises bid
    self._best_bid = price
elif side == "sell" and price < self._best_ask:   # ← only lowers ask
    self._best_ask = price
```

If the best bid drops or the best ask rises (which happens constantly), the WS handler **never updates**. It waits for the REST poll to correct, which is 3-10 seconds behind. This means after one initial snapshot, bid/ask can only get "tighter" via WS, never "wider" — creating the illusion of a stuck price.

**Failure 3 — Market window rotation resets don't clear stale prices.**

When a 5-minute window expires and a new market starts, `_fetch_current()` tries slugs `[0, 1, -1]`. If the new market isn't published yet (common — there's a few seconds of lag), the bot keeps using `_using_cached_market = True` with the OLD condition_id's bid/ask. The WS is still subscribed to the old market. Prices literally cannot update because the old market is resolved and the new one isn't subscribed yet.

### The Fix (Summary)

The price detection system needs a complete rewrite of the update path:
1. WS must handle **all** price changes, including removals (size=0 should trigger a full book re-request, not be ignored)
2. When WS silently exits due to auth errors, the REST poll interval must drop to 1-2 seconds (currently stays at 3s)
3. On market window rotation, prices must be reset to (0.0, 1.0) until the new market is confirmed
4. The `_clob_book_loop` should use the CLOB REST orderbook endpoint (`/book?token_id=X`), not the Gamma API, for real bid/ask

---

## 3. Full Bug & Issue Inventory

### CRITICAL (Breaks core functionality)

| # | Issue | File | Impact |
|---|-------|------|--------|
| C1 | WS book listener exits permanently on HTTP 404/401/403 | polymarket_gamma.py | Prices go stale after first auth error |
| C2 | `_handle_book_msg` only updates bid UP and ask DOWN | polymarket_gamma.py | Prices get stuck at initial values |
| C3 | size=0 book events ignored (top-of-book removals) | polymarket_gamma.py | Stale best bid/ask for up to 10s |
| C4 | No price reset on market window rotation | polymarket_gamma.py | Old market prices bleed into new window |
| C5 | `_clob_book_loop` uses Gamma API, not CLOB orderbook | polymarket_gamma.py | 3s+ latency on price updates |
| C6 | Live trader uses hardcoded `market_yes_price=0.50` | mm_enhanced_1.py | Fair value anchor is wrong for non-50/50 markets |

### HIGH (Significant impact on P&L or reliability)

| # | Issue | File | Impact |
|---|-------|------|--------|
| H1 | Paper trader uses 15% random fill probability | paper_trader.py | Unrealistic fill simulation |
| H2 | No NO-side quoting — bot only quotes YES bid/ask | paper_trader.py, live_order_manager.py | Misses half the market-making spread |
| H3 | `SideDataSnapshot` carries `market_best_bid/ask` but paper_trader falls back to fair-value-derived prices when they're 0.0/1.0 | paper_trader.py | Falls back to synthetic prices even when real ones are just stale |
| H4 | 300-second warmup blackout too aggressive | mm_enhanced_1.py | Misses 5+ minute windows of trading at every restart |
| H5 | No staleness guard on Gamma prices before passing to paper trader | mm_enhanced_1.py | Stale prices from 30+ seconds ago used for fill decisions |
| H6 | Backtesters (all 3 versions) use synthetic contract prices, not real historical Polymarket data | backtest/ | Backtests don't represent real market conditions |

### MEDIUM (Affects quality/performance)

| # | Issue | File | Impact |
|---|-------|------|--------|
| M1 | `mm_guide.md` is fully outdated (references old architecture) | mm_guide.md | Confusing for development/onboarding |
| M2 | Dashboard writes to `paper_mm_state.json` from main loop, not from paper_trader | mm_enhanced_1.py | State file can have stale/mismatched data |
| M3 | Chainlink feed polls every 10s but Chainlink updates every 27s or on 0.5% move | chainlink_feed.py | Wasted RPC calls OR missed updates |
| M4 | No reconnection logic in Binance feed if WS drops | binance_feed.py | BTC price goes stale, whole bot degrades |
| M5 | `HistoricalDataLoader.record_tick()` writes one JSONL line per second (86k lines/day) | mm_enhanced_1.py | Disk fills up, I/O bottleneck |
| M6 | Three separate backtester files with overlapping/conflicting logic | backtest/ | Confusing which one to use, results don't compare |

---

## 4. Architecture Flow (How Data Moves)

```
┌─────────────────────────────────────────────────────────────────────┐
│                        DATA SOURCES                                 │
│                                                                     │
│  Binance WS ──→ BTC price (real-time, ~100ms)                      │
│  Hyperliquid ──→ Oracle price + CVD + funding + OI + liq (3s poll)  │
│  Chainlink RPC → Settlement price (10s poll)                        │
│  Gamma API ────→ Market discovery + slug resolution (10s poll)      │
│  CLOB WS ──────→ YES bid/ask book updates (sub-second IF WORKING)  │
│  CLOB REST ────→ Gamma price fallback (3s poll)                     │
└─────────┬───────────────────────────────────────────────────────────┘
          │
          ▼
┌─────────────────────────────────────────────────────────────────────┐
│  SideDataSnapshot (assembled every 1s in main loop)                 │
│                                                                     │
│  btc_price, btc_change_1m/5m, btc_volatility_1m                    │
│  hl_oracle_price, hl_funding_rate, hl_open_interest                 │
│  cvd_signal, liq_signal, funding_signal, oi_signal                  │
│  chainlink_price                                                    │
│  market_spread, market_best_bid, market_best_ask  ← THESE GET STUCK│
│  seconds_to_expiry                                                  │
└─────────┬───────────────────────────────────────────────────────────┘
          │
          ▼
┌──────────────────────────┐     ┌──────────────────────────┐
│  EnhancedFairValueEngine │     │  ConfidenceCalculator    │
│  estimate(snapshot,      │     │  score(snapshot, ...)     │
│    market_yes_price)     │     │  → tier, score,          │
│  → fair_value (0-1)     │     │    size_multiplier,      │
│                          │     │    spread_multiplier     │
└─────────┬────────────────┘     └─────────┬────────────────┘
          │                                │
          ▼                                ▼
┌──────────────────────────────────────────────────────────────┐
│  EnhancedQuoteEngine.generate_quotes(fair_value, inventory) │
│  → yes_bid, yes_ask, spread, size, adjustments              │
│  (applies inventory skew, volatility widening, side-data)   │
└─────────┬────────────────────────────────────────────────────┘
          │
          ▼
┌──────────────────────────┐     ┌──────────────────────────┐
│  PaperTrader             │ OR  │  LiveOrderManager        │
│  process_cycle(quotes,   │     │  process_cycle(quotes,   │
│    snapshot, confidence)  │     │    snapshot, confidence,  │
│  → simulated fills       │     │    market_id, token_id)  │
│                          │     │  → real CLOB fills       │
└──────────────────────────┘     └──────────────────────────┘
```

**The critical bottleneck is at the top**: `market_best_bid` and `market_best_ask` flow from `polymarket_gamma.py` into the snapshot, into the paper trader's fill logic, and into the live order manager's quote prices. When these values get stuck, **everything downstream operates on stale data**.

---

## 5. TODO List (Prioritized)

### Phase 1: Fix Price Detection (URGENT — Do First)

- [ ] **T1.1** Rewrite `_handle_book_msg()` in `polymarket_gamma.py`:
  - Handle size=0 events by resetting bid/ask and requesting a full book snapshot
  - Update bid DOWN and ask UP (not just the tighter direction)
  - Track full top-3 levels so removals don't require REST correction

- [ ] **T1.2** Fix WS auth failure handling:
  - When HTTP 404/401/403 is received, don't permanently exit
  - Instead, fall back to aggressive REST polling (every 1s instead of 3s)
  - Log a clear warning that WS is unavailable

- [ ] **T1.3** Add CLOB REST orderbook endpoint as primary REST fallback:
  - Use `GET https://clob.polymarket.com/book?token_id={yes_token_id}` 
  - This returns the real orderbook, not Gamma's cached prices
  - Poll every 1-2 seconds when WS is down

- [ ] **T1.4** Reset prices on market window rotation:
  - When `_condition_id` changes, set `_best_bid = 0.0`, `_best_ask = 1.0`
  - Don't quote until new market has confirmed bid/ask
  - Add a `_price_confirmed` flag that gates the paper trader

- [ ] **T1.5** Add staleness guard in main loop:
  - If `gamma_feed._last_update` is older than 5 seconds, log a warning
  - If older than 15 seconds, force PAUSED confidence tier (don't quote on stale data)
  - Display staleness age in dashboard

- [ ] **T1.6** Fix hardcoded `market_yes_price=0.50` in live trader:
  - Use `gamma_feed.best_bid/best_ask` midpoint as anchor
  - Fall back to 0.50 only if no market data available

### Phase 2: Fix Backtesting (Important — Enables Strategy Validation)

- [ ] **T2.1** Consolidate backtester into ONE file:
  - Merge `backtest_v1failed.py`, `backtest_impV1.py`, `backtest_unified.py` into `backtest.py`
  - Use `backtest_unified.py` as the base (it has the best structure)
  - Delete the other two to avoid confusion

- [ ] **T2.2** Record real Polymarket bid/ask for backtesting:
  - `HistoricalDataLoader.record_tick()` already saves snapshots
  - Ensure `market_best_bid` and `market_best_ask` are included
  - After 1-2 weeks of data, backtests can use real prices instead of synthetic

- [ ] **T2.3** Fix synthetic price model in backtester:
  - Current model assumes 15% lag between BTC momentum and contract price
  - This is too optimistic — real lag varies from 1-60 seconds
  - Add configurable lag parameter and test sensitivity

- [ ] **T2.4** Add proper fee modeling to all backtests:
  - Import from `fees.py` (already exists, not used in all backtests)
  - Include gas costs per transaction
  - Track net P&L after all costs

- [ ] **T2.5** Implement Monte Carlo simulation:
  - Randomize fill timing and probability
  - Run 1000 iterations per config
  - Report median, P10, P90 outcomes (not just single-path results)

### Phase 3: Strategy Improvements

- [ ] **T3.1** Add NO-side quoting:
  - Currently only quotes YES bid/ask
  - Add NO bid/ask to capture spread on both contract sides
  - YES + NO always = $1.00 — this doubles spread capture opportunity

- [ ] **T3.2** Replace warmup blackout with gradual ramp:
  - Instead of 300s complete blackout, start with wide spreads (10¢+)
  - Narrow spreads as signals stabilize
  - Use confidence score to gate, not a timer

- [ ] **T3.3** Add multi-level quoting:
  - Instead of one bid + one ask, post 2-3 levels per side
  - Example: Bid 5sh @ $0.48, Bid 10sh @ $0.46
  - Captures more volume and provides better liquidity

- [ ] **T3.4** Implement volatility-adjusted spread:
  - Current spread widening uses a fixed multiplier
  - Should use realized BTC volatility (stddev of 1-min returns)
  - Higher vol → wider spread, lower vol → tighter spread

- [ ] **T3.5** Add adverse selection detector to paper trader:
  - Track if fills consistently move against you within 10 seconds
  - If 5+ consecutive adverse fills, widen spread or pause

### Phase 4: Update Documentation & Clean Up

- [ ] **T4.1** Rewrite `mm_guide.md` from scratch:
  - Update architecture diagram to match actual code
  - Document all config parameters with current defaults
  - Add troubleshooting section for common issues (stuck prices, WS failures)
  - Include setup instructions for Windows (the actual deployment OS)

- [ ] **T4.2** Add comprehensive logging for price updates:
  - Log every source of price change (WS book, WS price_change, REST Gamma, REST CLOB)
  - Include timestamps so you can trace exactly when/why prices went stale
  - Add a `--debug-prices` flag for verbose price tracking

- [ ] **T4.3** Clean up state file writes:
  - Paper trader should own its state file entirely
  - Main loop should not duplicate state writes with extra fields
  - Merge the two write paths into one

- [ ] **T4.4** Add health check endpoint to dashboard:
  - Show feed connection status (WS connected? REST polling? Last update age?)
  - Show price staleness for each feed
  - Alert when any feed is stale > 10s

### Phase 5: Production Hardening (Before Live Trading)

- [ ] **T5.1** Add crash recovery for live mode:
  - Persist open order IDs to disk every cycle
  - On restart, cancel orphaned orders (partially done in `_cancel_orphaned_orders`)
  - Verify wallet balance matches expected state

- [ ] **T5.2** Add rate limiting for CLOB API:
  - Cancel+Post every cycle = 4+ API calls per second
  - CLOB has rate limits — track and respect them
  - Implement exponential backoff on 429 errors

- [ ] **T5.3** Add kill switch:
  - Monitor a file/endpoint that can be toggled externally
  - If kill switch active, cancel all orders and stop quoting
  - Allows emergency stop without terminal access

---

## 6. Backtesting Strategy Recommendations

The current backtester failures stem from testing a **directional** strategy when the bot is a **market maker**. Here's what to test:

### Backtest A: Spread Sensitivity

Test: How does P&L change with spread width from 2¢ to 10¢?

Expected result: There's a sweet spot where fills per hour × spread per fill is maximized. Too tight = adverse selection losses. Too wide = no fills.

### Backtest B: Inventory Skew Factor

Test: Sweep `inventory_skew_factor` from 0.001 to 0.010.

Expected result: Higher skew = faster inventory reduction but worse fill prices. Find the value where max inventory stays below 50 shares while spread P&L remains positive.

### Backtest C: Fill Probability vs Market Conditions

Test: Does the 15% fill probability model match reality? Record real fills for 1 week and compare.

Expected result: Fill probability likely varies by time of day, volatility regime, and spread width. A dynamic fill model would be more accurate.

### Backtest D: Volatility Regime Detection

Test: Classify BTC into low/medium/high volatility regimes. Run the bot with different configs per regime.

Expected result: Tight spreads in low-vol, wide spreads in high-vol should outperform a single config.

---

## 7. Quick Reference: What Each Config Parameter Does

| Parameter | Default | What It Controls | Tune When |
|-----------|---------|-----------------|-----------|
| `base_spread_pct` | 0.04 (4¢) | Distance between bid and ask | Too few fills (widen) or too many adverse fills (tighten) |
| `quote_size_shares` | 1.0 | Shares per quote | Scale up after profitable paper trading |
| `inventory_skew_factor` | 0.002 | How much inventory shifts quotes | Inventory drifts too far from zero |
| `max_inventory` | 15.0 | Hard cap on net position | Risk tolerance |
| `starting_capital` | 15.0 | Paper trading capital | Match intended live capital |
| `FILL_PROBABILITY` | 0.15 (15%) | Paper fill simulation rate | Calibrate against real fill data |
| `_WARMUP_SECONDS` | 300 (5 min) | No-quote period after start | Reduce once price feeds are proven stable |

---

## 8. Summary: Immediate Action Items

1. **Fix `polymarket_gamma.py`** — This is the root cause of stuck prices. Rewrite the WS handler and REST fallback to provide sub-3-second price updates at all times.

2. **Consolidate backtests** — Delete `backtest_v1failed.py` and `backtest_impV1.py`. Use `backtest_unified.py` as the single source of truth.

3. **Add NO-side quoting** — You're leaving half the spread on the table by only quoting YES.

4. **Rewrite `mm_guide.md`** — It references old architecture and will confuse any future development.

5. **Start recording real Polymarket data** — Every day of real bid/ask recordings makes backtests more reliable. Start this immediately even before fixing anything else.
# Market Maker Bot — Full Structure Analysis & TODO List

**Path:** `C:\Users\Admin\OneDrive\Documentos\claude-bot\market_maker`  
**Date:** April 5, 2026

---

## 1. Current File Structure Map

```
market_maker/
├── mm_enhanced_1.py          ← MAIN ENTRY POINT (paper, live, signals, resolver, backtest)
├── polymarket_gamma.py       ← Polymarket market discovery + YES/NO price feed
├── hyperliquid_api.py        ← Derivatives side-data (CVD, funding, OI, liq)
├── binance_feed.py           ← BTC price via Binance WebSocket (imported as BinanceBTCFeed)
├── chainlink_feed.py         ← Settlement price feed (Polygon RPC)
├── paper_trader.py           ← Simulated fill engine for paper mode
├── live_order_manager.py     ← Real CLOB order placement (py-clob-client)
├── confidence.py             ← Confidence scoring / tier system
├── fees.py                   ← Polymarket dynamic fee model
├── alerting.py               ← Telegram/Discord notifications
├── mm_dashboard.py           ← Terminal + web dashboard (aiohttp)
├── mm_guide.md               ← Guide doc (OUTDATED — needs full rewrite)
├── data/
│   ├── paper_mm_state.json   ← Persisted paper state
│   ├── paper_mm_fills.json   ← Fill history
│   └── mm_historical.jsonl   ← Recorded ticks for backtesting
└── backtest/
    ├── BOT_ANALYSIS.md       ← Previous analysis (still relevant)
    ├── backtest_v1failed.py  ← Directional backtester (failed approach)
    ├── backtest_impV1.py     ← Improved MM backtester
    ├── backtest_unified.py   ← Unified backtester with grid search
    └── polybot_dashboard.jsx ← React dashboard (for directional bot)
```

---

## 2. ROOT CAUSE: Why YES/NO Prices Get Stuck

**This is the #1 bug you're experiencing.** The bot detects one price and then keeps logging the same value. Here's exactly why:

### Bug Chain (3 interacting failures)

**Failure 1 — CLOB WebSocket silently dies, REST fallback is too slow.**

In `polymarket_gamma.py`, the WS book listener connects to `wss://clob.polymarket.com/ws/` and subscribes to the `book` channel. But there are two critical problems:

```
_ws_book_listener() → connects → subscribes to condition_id
  BUT:
  1. If HTTP 404/401/403 → it RETURNS (exits forever), no retry
  2. price_change events with size==0 are SKIPPED ("REST will correct within 10s")
  3. The REST correction loop (_clob_book_loop) only runs every 3 seconds
     AND it uses the Gamma REST API, not the CLOB orderbook API
```

When the WS connection gets an auth error (very common — the CLOB book channel often requires credentials), the listener **silently exits permanently** with just an info log. From that point, prices are only updated by:
- `_clob_book_loop()` — polls Gamma REST every 3s, but only gets `bestBid`/`bestAsk` from the Gamma API response, which itself can be stale (Gamma caches aggressively)
- `_poll_loop()` → `_fetch_current()` — runs every 10s, fetches market data including `outcomePrices`

**Failure 2 — `_handle_book_msg()` only updates prices UPWARD for bids and DOWNWARD for asks.**

```python
# In price_change handler:
if side == "buy" and price > self._best_bid:     # ← only raises bid
    self._best_bid = price
elif side == "sell" and price < self._best_ask:   # ← only lowers ask
    self._best_ask = price
```

If the best bid drops or the best ask rises (which happens constantly), the WS handler **never updates**. It waits for the REST poll to correct, which is 3-10 seconds behind. This means after one initial snapshot, bid/ask can only get "tighter" via WS, never "wider" — creating the illusion of a stuck price.

**Failure 3 — Market window rotation resets don't clear stale prices.**

When a 5-minute window expires and a new market starts, `_fetch_current()` tries slugs `[0, 1, -1]`. If the new market isn't published yet (common — there's a few seconds of lag), the bot keeps using `_using_cached_market = True` with the OLD condition_id's bid/ask. The WS is still subscribed to the old market. Prices literally cannot update because the old market is resolved and the new one isn't subscribed yet.

### The Fix (Summary)

The price detection system needs a complete rewrite of the update path:
1. WS must handle **all** price changes, including removals (size=0 should trigger a full book re-request, not be ignored)
2. When WS silently exits due to auth errors, the REST poll interval must drop to 1-2 seconds (currently stays at 3s)
3. On market window rotation, prices must be reset to (0.0, 1.0) until the new market is confirmed
4. The `_clob_book_loop` should use the CLOB REST orderbook endpoint (`/book?token_id=X`), not the Gamma API, for real bid/ask

---

## 3. Full Bug & Issue Inventory

### CRITICAL (Breaks core functionality)

| # | Issue | File | Impact |
|---|-------|------|--------|
| C1 | WS book listener exits permanently on HTTP 404/401/403 | polymarket_gamma.py | Prices go stale after first auth error |
| C2 | `_handle_book_msg` only updates bid UP and ask DOWN | polymarket_gamma.py | Prices get stuck at initial values |
| C3 | size=0 book events ignored (top-of-book removals) | polymarket_gamma.py | Stale best bid/ask for up to 10s |
| C4 | No price reset on market window rotation | polymarket_gamma.py | Old market prices bleed into new window |
| C5 | `_clob_book_loop` uses Gamma API, not CLOB orderbook | polymarket_gamma.py | 3s+ latency on price updates |
| C6 | Live trader uses hardcoded `market_yes_price=0.50` | mm_enhanced_1.py | Fair value anchor is wrong for non-50/50 markets |

### HIGH (Significant impact on P&L or reliability)

| # | Issue | File | Impact |
|---|-------|------|--------|
| H1 | Paper trader uses 15% random fill probability | paper_trader.py | Unrealistic fill simulation |
| H2 | No NO-side quoting — bot only quotes YES bid/ask | paper_trader.py, live_order_manager.py | Misses half the market-making spread |
| H3 | `SideDataSnapshot` carries `market_best_bid/ask` but paper_trader falls back to fair-value-derived prices when they're 0.0/1.0 | paper_trader.py | Falls back to synthetic prices even when real ones are just stale |
| H4 | 300-second warmup blackout too aggressive | mm_enhanced_1.py | Misses 5+ minute windows of trading at every restart |
| H5 | No staleness guard on Gamma prices before passing to paper trader | mm_enhanced_1.py | Stale prices from 30+ seconds ago used for fill decisions |
| H6 | Backtesters (all 3 versions) use synthetic contract prices, not real historical Polymarket data | backtest/ | Backtests don't represent real market conditions |

### MEDIUM (Affects quality/performance)

| # | Issue | File | Impact |
|---|-------|------|--------|
| M1 | `mm_guide.md` is fully outdated (references old architecture) | mm_guide.md | Confusing for development/onboarding |
| M2 | Dashboard writes to `paper_mm_state.json` from main loop, not from paper_trader | mm_enhanced_1.py | State file can have stale/mismatched data |
| M3 | Chainlink feed polls every 10s but Chainlink updates every 27s or on 0.5% move | chainlink_feed.py | Wasted RPC calls OR missed updates |
| M4 | No reconnection logic in Binance feed if WS drops | binance_feed.py | BTC price goes stale, whole bot degrades |
| M5 | `HistoricalDataLoader.record_tick()` writes one JSONL line per second (86k lines/day) | mm_enhanced_1.py | Disk fills up, I/O bottleneck |
| M6 | Three separate backtester files with overlapping/conflicting logic | backtest/ | Confusing which one to use, results don't compare |

---

## 4. Architecture Flow (How Data Moves)

```
┌─────────────────────────────────────────────────────────────────────┐
│                        DATA SOURCES                                 │
│                                                                     │
│  Binance WS ──→ BTC price (real-time, ~100ms)                      │
│  Hyperliquid ──→ Oracle price + CVD + funding + OI + liq (3s poll)  │
│  Chainlink RPC → Settlement price (10s poll)                        │
│  Gamma API ────→ Market discovery + slug resolution (10s poll)      │
│  CLOB WS ──────→ YES bid/ask book updates (sub-second IF WORKING)  │
│  CLOB REST ────→ Gamma price fallback (3s poll)                     │
└─────────┬───────────────────────────────────────────────────────────┘
          │
          ▼
┌─────────────────────────────────────────────────────────────────────┐
│  SideDataSnapshot (assembled every 1s in main loop)                 │
│                                                                     │
│  btc_price, btc_change_1m/5m, btc_volatility_1m                    │
│  hl_oracle_price, hl_funding_rate, hl_open_interest                 │
│  cvd_signal, liq_signal, funding_signal, oi_signal                  │
│  chainlink_price                                                    │
│  market_spread, market_best_bid, market_best_ask  ← THESE GET STUCK│
│  seconds_to_expiry                                                  │
└─────────┬───────────────────────────────────────────────────────────┘
          │
          ▼
┌──────────────────────────┐     ┌──────────────────────────┐
│  EnhancedFairValueEngine │     │  ConfidenceCalculator    │
│  estimate(snapshot,      │     │  score(snapshot, ...)     │
│    market_yes_price)     │     │  → tier, score,          │
│  → fair_value (0-1)     │     │    size_multiplier,      │
│                          │     │    spread_multiplier     │
└─────────┬────────────────┘     └─────────┬────────────────┘
          │                                │
          ▼                                ▼
┌──────────────────────────────────────────────────────────────┐
│  EnhancedQuoteEngine.generate_quotes(fair_value, inventory) │
│  → yes_bid, yes_ask, spread, size, adjustments              │
│  (applies inventory skew, volatility widening, side-data)   │
└─────────┬────────────────────────────────────────────────────┘
          │
          ▼
┌──────────────────────────┐     ┌──────────────────────────┐
│  PaperTrader             │ OR  │  LiveOrderManager        │
│  process_cycle(quotes,   │     │  process_cycle(quotes,   │
│    snapshot, confidence)  │     │    snapshot, confidence,  │
│  → simulated fills       │     │    market_id, token_id)  │
│                          │     │  → real CLOB fills       │
└──────────────────────────┘     └──────────────────────────┘
```

**The critical bottleneck is at the top**: `market_best_bid` and `market_best_ask` flow from `polymarket_gamma.py` into the snapshot, into the paper trader's fill logic, and into the live order manager's quote prices. When these values get stuck, **everything downstream operates on stale data**.

---

## 5. TODO List (Prioritized)

### Phase 1: Fix Price Detection (URGENT — Do First)

- [FIXED ] **T1.1** Rewrite `_handle_book_msg()` in `polymarket_gamma.py`:
  - Handle size=0 events by resetting bid/ask and requesting a full book snapshot
  - Update bid DOWN and ask UP (not just the tighter direction)
  - Track full top-3 levels so removals don't require REST correction

- [ ] **T1.2** Fix WS auth failure handling:
  - When HTTP 404/401/403 is received, don't permanently exit
  - Instead, fall back to aggressive REST polling (every 1s instead of 3s)
  - Log a clear warning that WS is unavailable

- [ ] **T1.3** Add CLOB REST orderbook endpoint as primary REST fallback:
  - Use `GET https://clob.polymarket.com/book?token_id={yes_token_id}` 
  - This returns the real orderbook, not Gamma's cached prices
  - Poll every 1-2 seconds when WS is down

- [ ] **T1.4** Reset prices on market window rotation:
  - When `_condition_id` changes, set `_best_bid = 0.0`, `_best_ask = 1.0`
  - Don't quote until new market has confirmed bid/ask
  - Add a `_price_confirmed` flag that gates the paper trader

- [ ] **T1.5** Add staleness guard in main loop:
  - If `gamma_feed._last_update` is older than 5 seconds, log a warning
  - If older than 15 seconds, force PAUSED confidence tier (don't quote on stale data)
  - Display staleness age in dashboard

- [ ] **T1.6** Fix hardcoded `market_yes_price=0.50` in live trader:
  - Use `gamma_feed.best_bid/best_ask` midpoint as anchor
  - Fall back to 0.50 only if no market data available

### Phase 2: Fix Backtesting (Important — Enables Strategy Validation)

- [ ] **T2.1** Consolidate backtester into ONE file:
  - Merge `backtest_v1failed.py`, `backtest_impV1.py`, `backtest_unified.py` into `backtest.py`
  - Use `backtest_unified.py` as the base (it has the best structure)
  - Delete the other two to avoid confusion

- [ ] **T2.2** Record real Polymarket bid/ask for backtesting:
  - `HistoricalDataLoader.record_tick()` already saves snapshots
  - Ensure `market_best_bid` and `market_best_ask` are included
  - After 1-2 weeks of data, backtests can use real prices instead of synthetic

- [ ] **T2.3** Fix synthetic price model in backtester:
  - Current model assumes 15% lag between BTC momentum and contract price
  - This is too optimistic — real lag varies from 1-60 seconds
  - Add configurable lag parameter and test sensitivity

- [ ] **T2.4** Add proper fee modeling to all backtests:
  - Import from `fees.py` (already exists, not used in all backtests)
  - Include gas costs per transaction
  - Track net P&L after all costs

- [ ] **T2.5** Implement Monte Carlo simulation:
  - Randomize fill timing and probability
  - Run 1000 iterations per config
  - Report median, P10, P90 outcomes (not just single-path results)

### Phase 3: Strategy Improvements

- [ ] **T3.1** Add NO-side quoting:
  - Currently only quotes YES bid/ask
  - Add NO bid/ask to capture spread on both contract sides
  - YES + NO always = $1.00 — this doubles spread capture opportunity

- [ ] **T3.2** Replace warmup blackout with gradual ramp:
  - Instead of 300s complete blackout, start with wide spreads (10¢+)
  - Narrow spreads as signals stabilize
  - Use confidence score to gate, not a timer

- [ ] **T3.3** Add multi-level quoting:
  - Instead of one bid + one ask, post 2-3 levels per side
  - Example: Bid 5sh @ $0.48, Bid 10sh @ $0.46
  - Captures more volume and provides better liquidity

- [ ] **T3.4** Implement volatility-adjusted spread:
  - Current spread widening uses a fixed multiplier
  - Should use realized BTC volatility (stddev of 1-min returns)
  - Higher vol → wider spread, lower vol → tighter spread

- [ ] **T3.5** Add adverse selection detector to paper trader:
  - Track if fills consistently move against you within 10 seconds
  - If 5+ consecutive adverse fills, widen spread or pause

### Phase 4: Update Documentation & Clean Up

- [ ] **T4.1** Rewrite `mm_guide.md` from scratch:
  - Update architecture diagram to match actual code
  - Document all config parameters with current defaults
  - Add troubleshooting section for common issues (stuck prices, WS failures)
  - Include setup instructions for Windows (the actual deployment OS)

- [ ] **T4.2** Add comprehensive logging for price updates:
  - Log every source of price change (WS book, WS price_change, REST Gamma, REST CLOB)
  - Include timestamps so you can trace exactly when/why prices went stale
  - Add a `--debug-prices` flag for verbose price tracking

- [ ] **T4.3** Clean up state file writes:
  - Paper trader should own its state file entirely
  - Main loop should not duplicate state writes with extra fields
  - Merge the two write paths into one

- [ ] **T4.4** Add health check endpoint to dashboard:
  - Show feed connection status (WS connected? REST polling? Last update age?)
  - Show price staleness for each feed
  - Alert when any feed is stale > 10s

### Phase 5: Production Hardening (Before Live Trading)

- [ ] **T5.1** Add crash recovery for live mode:
  - Persist open order IDs to disk every cycle
  - On restart, cancel orphaned orders (partially done in `_cancel_orphaned_orders`)
  - Verify wallet balance matches expected state

- [ ] **T5.2** Add rate limiting for CLOB API:
  - Cancel+Post every cycle = 4+ API calls per second
  - CLOB has rate limits — track and respect them
  - Implement exponential backoff on 429 errors

- [ ] **T5.3** Add kill switch:
  - Monitor a file/endpoint that can be toggled externally
  - If kill switch active, cancel all orders and stop quoting
  - Allows emergency stop without terminal access

---

## 6. Backtesting Strategy Recommendations

The current backtester failures stem from testing a **directional** strategy when the bot is a **market maker**. Here's what to test:

### Backtest A: Spread Sensitivity

Test: How does P&L change with spread width from 2¢ to 10¢?

Expected result: There's a sweet spot where fills per hour × spread per fill is maximized. Too tight = adverse selection losses. Too wide = no fills.

### Backtest B: Inventory Skew Factor

Test: Sweep `inventory_skew_factor` from 0.001 to 0.010.

Expected result: Higher skew = faster inventory reduction but worse fill prices. Find the value where max inventory stays below 50 shares while spread P&L remains positive.

### Backtest C: Fill Probability vs Market Conditions

Test: Does the 15% fill probability model match reality? Record real fills for 1 week and compare.

Expected result: Fill probability likely varies by time of day, volatility regime, and spread width. A dynamic fill model would be more accurate.

### Backtest D: Volatility Regime Detection

Test: Classify BTC into low/medium/high volatility regimes. Run the bot with different configs per regime.

Expected result: Tight spreads in low-vol, wide spreads in high-vol should outperform a single config.

---

## 7. Quick Reference: What Each Config Parameter Does

| Parameter | Default | What It Controls | Tune When |
|-----------|---------|-----------------|-----------|
| `base_spread_pct` | 0.04 (4¢) | Distance between bid and ask | Too few fills (widen) or too many adverse fills (tighten) |
| `quote_size_shares` | 1.0 | Shares per quote | Scale up after profitable paper trading |
| `inventory_skew_factor` | 0.002 | How much inventory shifts quotes | Inventory drifts too far from zero |
| `max_inventory` | 15.0 | Hard cap on net position | Risk tolerance |
| `starting_capital` | 15.0 | Paper trading capital | Match intended live capital |
| `FILL_PROBABILITY` | 0.15 (15%) | Paper fill simulation rate | Calibrate against real fill data |
| `_WARMUP_SECONDS` | 300 (5 min) | No-quote period after start | Reduce once price feeds are proven stable |

---

## 8. Summary: Immediate Action Items

1. **Fix `polymarket_gamma.py`** — This is the root cause of stuck prices. Rewrite the WS handler and REST fallback to provide sub-3-second price updates at all times.

2. **Consolidate backtests** — Delete `backtest_v1failed.py` and `backtest_impV1.py`. Use `backtest_unified.py` as the single source of truth.

3. **Add NO-side quoting** — You're leaving half the spread on the table by only quoting YES.

4. **Rewrite `mm_guide.md`** — It references old architecture and will confuse any future development.

5. **Start recording real Polymarket data** — Every day of real bid/ask recordings makes backtests more reliable. Start this immediately even before fixing anything else.
