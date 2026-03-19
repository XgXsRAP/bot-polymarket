# Polymarket BTC Bot — Complete Guide

## What Is This?

A high-frequency trading bot that trades **BTC 5-minute prediction markets** on [Polymarket](https://polymarket.com).

Instead of trading BTC directly, it trades contracts that ask:
> "Will BTC be higher in 5 minutes?"

The bot buys YES contracts when BTC momentum is strongly up and the market is underpricing that probability — and NO contracts when momentum is down. It scalps the difference between the market's implied probability and its estimated fair value.

**Target:** 0.10–0.80% profit per trade, many small trades per day.

---

## How It Works — Architecture

```
Binance WebSocket  →  BTCPriceFeed   →  SignalEngine
                                              ↓
Polymarket WS/REST →  PolymarketFeed  →  (edge detected?)
                                              ↓
                      ClaudeBot AI    →  RiskManager  →  Trader (Paper or Live)
                                                               ↓
                      PositionMonitor  ←────────────────  open trade
                             ↓
                        exit on: profit target / stop loss / timeout / expiry
```

### Components

| Component | Role |
|---|---|
| `BTCPriceFeed` | Connects to Binance WebSocket — real-time BTC price + 1m/5m momentum |
| `PolymarketFeed` | Polls Gamma API every 30s to find BTC 5-min markets. Updates prices via CLOB WebSocket |
| `SignalEngine` | Estimates fair probability from BTC momentum. Compares to market price. If edge > 0.3% → signal |
| `RiskManager` | Controls position size, daily loss limit, and circuit breaker |
| `PaperTrader` | Simulates fills using real prices. Saves results to `data/paper_trades.json` |
| `LiveTrader` | Posts real BUY/SELL orders on Polygon via Polymarket CLOB API |
| `ClaudeBot` | (Optional) Calls Claude AI every 60s to classify market regime and scale position sizes |
| `PositionMonitor` | Checks every 100ms whether an open trade should exit |

### Signal Logic (simplified)

```
1m momentum → 70% weight  ┐
5m momentum → 30% weight  ┘→ fair_value (0.0–1.0)

yes_edge = fair_value - market_yes_price
no_edge  = (1 - fair_value) - market_no_price

if max(yes_edge, no_edge) > 0.3% → enter trade
```

### Exit Conditions (in priority order)

| Reason | Trigger |
|---|---|
| `profit_target` | Unrealized P&L ≥ 0.10% — take the win |
| `stop_loss` | Unrealized P&L ≤ −0.50% — cut the loss |
| `expiry_approaching` | < 30 seconds to market expiry |
| `max_hold_timeout` | Held > 4.5 minutes with no exit |
| `signal_reversal` | BTC reverses strongly against the trade and P&L is negative |

---

## Setup

### 1. Install Python dependencies

```bash
pip install websockets aiohttp loguru python-dotenv anthropic
pip install streamlit plotly pandas requests
pip install py-clob-client   # only needed for live trading
```

Optional (Linux only — 2–3x faster event loop):
```bash
pip install uvloop
```

### 2. Create your `.env` file

```bash
cp .env.template .env
```

Open `.env` and fill in your values. For **paper trading** you only need:
```
PAPER_TRADING=true
INITIAL_CAPITAL=1000
```

Everything else can stay blank until you go live.

### 3. Create the data and logs folders

The bot creates these automatically on first run, but you can also do it manually:
```bash
mkdir data logs
```

---

## Running the Bot

### Paper trading (safe — no real money)

Open **two terminals** in the project folder:

**Terminal 1 — the bot:**
```bash
python bot.py --mode paper
```

**Terminal 2 — the dashboard:**
```bash
streamlit run dash-board.py
```

Then open your browser at `http://localhost:8501`

### Live trading (real money — read warnings below first)

```bash
python bot.py --mode live
```

---

## Dashboard Controls

The dashboard at `http://localhost:8501` gives you:

| Control | What it does |
|---|---|
| **▶ Start** | Resumes the bot if it was paused |
| **⏸ Pause** | Stops new trades from opening (existing ones stay open) |
| **■ Stop Bot** | Shuts down the bot cleanly |
| **Enable ClaudeBot** | Toggles AI market regime analysis on/off without restarting |

### Dashboard panels

| Panel | Description |
|---|---|
| Health banner | Green = running, yellow = paused, red = stopped |
| Capital / BTC / Win Rate | Live metrics updated every second |
| P&L History tab | Cumulative P&L curve from all closed trades |
| BTC 1m Candles tab | Live candlestick chart from Binance (refreshes every 60s) |
| Open Positions | Live view of trades in progress with unrealized P&L |
| Momentum Gauges | Dual speedometer — 1m and 5m BTC % change |
| Win/Loss Donut | Overall win rate |
| Exit Reasons bar | How each trade ended (profit target, stop loss, timeout, etc.) |
| Risk Meters | Daily loss used, consecutive losses, position slots used |
| Session Stats | Total P&L, profit factor, best/worst trade, go-live checklist |
| Signal Log | Last 50 lines of the bot log — color coded |
| Closed Trades table | Last 50 closed trades — sortable |
| Hold Duration histogram | Distribution of how long trades lasted |
| ⬇ Export CSV | Download all closed trades as a spreadsheet |

---

## Command Line Arguments

```bash
python bot.py --mode paper   # paper trading (default)
python bot.py --mode live    # live trading (requires .env credentials)
python bot.py --help         # show help
```

---

## Configuration Parameters

All key parameters are in the `Config` dataclass at the top of `bot.py`. You can change them directly in the file.

### Risk parameters

| Parameter | Default | What it controls |
|---|---|---|
| `max_trade_pct` | `0.005` (0.5%) | Maximum capital per trade |
| `daily_loss_limit_pct` | `0.02` (2%) | Stops trading for the day if hit |
| `min_edge_required` | `0.003` (0.3%) | Minimum edge needed to enter a trade |
| `min_profit_target` | `0.001` (0.10%) | Exit when unrealized P&L hits this |
| `max_hold_seconds` | `270` (4.5 min) | Force-exit if trade hasn't resolved |
| `consecutive_loss_limit` | `5` | Number of losses before circuit breaker fires |
| `circuit_breaker_pause` | `600` (10 min) | How long the circuit breaker pauses trading |

### Signal parameters (inside `SignalEngine.calculate_fair_value`)

| Parameter | Default | What it controls |
|---|---|---|
| 1m momentum multiplier | `10` | How aggressively 1m momentum shifts fair value |
| 5m momentum multiplier | `5` | How aggressively 5m momentum shifts fair value |
| 1m weight | `0.7` | Weight of 1m momentum in final fair value |
| 5m weight | `0.3` | Weight of 5m momentum in final fair value |
| Max 1m adjustment | `±0.15` | Caps the 1m contribution to fair value |
| Max 5m adjustment | `±0.10` | Caps the 5m contribution to fair value |

### Speed parameters

| Parameter | Default | What it controls |
|---|---|---|
| `target_latency_ms` | `100` | Main loop cycle time in milliseconds |
| `ws_reconnect_delay` | `1.0` | Seconds to wait before reconnecting a dropped WebSocket |

### Position limits

| Parameter | Default | What it controls |
|---|---|---|
| Max concurrent positions | `3` | Hard limit — set in `_scan_and_trade` |
| ClaudeBot cache | `60s` | How often the AI re-analyzes the market regime |

---

## Tuning for Better Success Rate

### If win rate is below 50%

- **Raise `min_edge_required`** from 0.3% to 0.5% or 0.7% — you'll trade less but only on higher-confidence signals
- **Lower momentum multipliers** (currently 10 and 5) — the signal may be overclaiming edge in low-momentum markets
- **Enable ClaudeBot** — the AI multiplier will reduce position sizes in sideways/volatile regimes

### If too few trades are happening

- **Lower `min_edge_required`** to 0.2% — more trades, but lower average quality
- **Raise max concurrent positions** from 3 to 5
- **Lower `min_profit_target`** to 0.05% — exits sooner but catches more small wins

### If stop losses are hit frequently

- **Widen stop loss** from −0.5% to −0.8% — gives trades more breathing room
- **Raise `min_edge_required`** — bad entries cause stop-loss hits
- **Check signal logic** — make sure BTC feed latency is low (shown in dashboard header)

### If trades are timing out (max_hold_timeout is the most common exit reason)

- The signal edge isn't materializing — the market isn't moving toward fair value
- Lower `max_hold_seconds` to 120s and lower `min_profit_target` to 0.05% to take smaller wins faster
- Or raise `min_edge_required` to only enter when the edge is very clear

### ClaudeBot tuning

When enabled, Claude classifies the market as:

| Regime | Multiplier range | Meaning |
|---|---|---|
| `trending_up` | 1.2–1.5× | Strong uptrend — increase YES position sizes |
| `trending_down` | 1.2–1.5× | Strong downtrend — increase NO position sizes |
| `sideways` | 0.5–0.8× | No direction — trade smaller or skip |
| `volatile` | 0.6–0.9× | Choppy — reduce size, higher chance of stop-loss |

Cost: approximately $0.50–$2.00/day at default (200 tokens every 60 seconds).

---

## Key Numbers to Watch

| Metric | Target | Warning |
|---|---|---|
| Win rate | > 52% | Below 50% = strategy not working |
| Profit factor | > 1.5 | Below 1.0 = losing money overall |
| Daily P&L | > 0% | −2% = daily loss limit, bot stops |
| Consecutive losses | < 3 | 5 = circuit breaker fires |
| Latency | < 100ms | > 200ms = competitive disadvantage |
| Exit reason — profit_target | Majority | Good |
| Exit reason — stop_loss | < 25% of trades | Bad if dominant |
| Exit reason — max_hold_timeout | < 30% of trades | Signal not working if dominant |

### Go-live checklist (shown in dashboard)

Before switching to live trading, the dashboard checks:
- Win rate > 52% over at least 50 paper trades
- No daily loss limit hit in the last session
- Profit factor > 1.5

---

## Paper Trading vs Live Trading

### Paper trading (default)

```
PAPER_TRADING=true   ← in .env
```

- Uses **real Polymarket prices** but simulates fills
- Adds a 50ms artificial delay to simulate order latency
- Saves all trades to `data/paper_trades.json`
- Zero financial risk
- API credentials not required

### Switching to live trading

**Step 1 — Validate paper results:**
Run at least 50 paper trades with win rate > 52% and profit factor > 1.5.

**Step 2 — Set up your Polymarket account:**
1. Go to [polymarket.com](https://polymarket.com) and connect MetaMask
2. Switch MetaMask to **Polygon network**
3. Fund your wallet with USDC on Polygon (bridge from Ethereum or buy directly)
4. Go to Profile → API Keys → Generate API key
5. Copy the key, secret, and passphrase

**Step 3 — Fill in `.env`:**
```
PAPER_TRADING=false
INITIAL_CAPITAL=<your actual USDC balance>
POLYMARKET_API_KEY=<from polymarket profile>
POLYMARKET_API_SECRET=<from polymarket profile>
POLYMARKET_API_PASSPHRASE=<from polymarket profile>
POLYMARKET_PRIVATE_KEY=<your wallet private key>
```

**Step 4 — Start small:**
```bash
python bot.py --mode live
```

Start with a small capital (e.g., $100) and monitor for the first hour before scaling up.

> **Warning:** Live trading uses real USDC on the Polygon blockchain. Transactions are irreversible. Start in paper mode and only go live after sustained paper profitability.

---

## Do You Need a `.env` File?

**Yes, always** — even for paper trading. The bot loads `.env` on startup via `python-dotenv`.

For paper trading, the minimum `.env` you need is:

```
PAPER_TRADING=true
INITIAL_CAPITAL=1000
```

Copy `.env.template` to `.env`:
```bash
cp .env.template .env
```

The `.env` file is **never committed to git** — add it to `.gitignore`:
```
.env
data/
logs/
```

---

## V1 Known Limitations & Planned Improvements

### Critical (must fix before going live)

| Issue | Description | Fix |
|---|---|---|
| No backtesting | Strategy parameters were chosen by intuition — no historical validation | Build a backtester against Polymarket historical data |
| Volume normalization missing | Signal ignores volume — high-volume moves should be weighted more | Normalize `change_1m` by rolling average volume |
| No fill confirmation | Live mode posts the order but doesn't check if it actually filled | Poll `GET /order/{id}` after posting and only record trade if filled |
| State lost on crash | Open trades live in memory — if the bot crashes, positions are orphaned | Persist `open_trades` to `data/open_trades.json` and reload on startup |

### Signal quality

| Issue | Description | Improvement |
|---|---|---|
| Aggressive momentum scaling | `mom * 10` can push fair value to extremes on small moves | Calibrate multipliers against historical edge/outcome data |
| No liquidity check | Bot may enter thin markets with wide bid-ask spreads | Filter out markets where `ask - bid > 0.02` |
| Hard-coded stop loss | −0.5% regardless of signal confidence or market regime | Scale stop loss with confidence: high confidence → tighter stop |
| Market keyword matching | Filters by "btc"/"5 min" — could miss or misclassify markets | Use `conditionType` and `resolution` fields from Gamma API instead |

### Infrastructure

| Issue | Description | Improvement |
|---|---|---|
| Single process | All capital in one bot — no failover | Deploy two instances with separate capital splits |
| In-memory rate limiter | Resets on restart | Replace with Redis-backed rate tracking |
| No alerting | You only know if something went wrong by checking the dashboard | Add Telegram/Discord alert on circuit breaker or daily loss limit |
| No position averaging | Cannot add to a winning position | Allow a second entry on the same market at a better price |

---

## File Structure

```
claude-bot/
├── bot.py               — Main trading bot (run this)
├── dash-board.py        — Streamlit monitoring dashboard
├── .env.template        — Environment variable template (copy to .env)
├── .env                 — Your actual secrets (never commit this)
├── GUIDE.md             — This file
├── roadmap.md           — Setup and deployment roadmap
├── data/
│   ├── bot_state.json   — Live bot state (written every second)
│   ├── bot_commands.json — Dashboard → bot commands
│   └── paper_trades.json — Paper trading history
└── logs/
    ├── bot.log          — Full debug log (rotates at 100MB)
    └── trades.log       — Trade entries/exits only
```

---

## Quick Reference

```bash
# Install dependencies
pip install websockets aiohttp loguru python-dotenv anthropic streamlit plotly pandas requests

# Setup
cp .env.template .env          # then edit .env with your values

# Run (paper mode — safe)
python bot.py --mode paper      # Terminal 1
streamlit run dash-board.py    # Terminal 2 → open http://localhost:8501

# Run (live mode — real money)
python bot.py --mode live

# Stop the bot
Ctrl+C in Terminal 1
# or click ■ Stop Bot in the dashboard
```
