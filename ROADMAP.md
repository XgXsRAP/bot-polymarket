# 🗺️ POLYMARKET BTC BOT — FULL ROADMAP
### Project: High-Frequency BTC 5-Min Prediction Trader
---

## ⏱️ ESTIMATED TIME TO COMPLETE
| Phase | Task | Time Estimate |
|-------|------|---------------|
| 1 | Environment Setup | 2–3 hours |
| 2 | API Keys & Accounts | 1–2 hours |
| 3 | Paper Trading Test | 2–5 days |
| 4 | Live Bot (small capital) | 1–2 days after paper |
| 5 | Optimization & Tuning | Ongoing |
| **TOTAL** | **First trade live** | **~1 week** |

---

## 🖥️ SECTION 2 — BEST OS TO USE

### ✅ RECOMMENDED: Ubuntu 22.04 LTS (Linux)
**Why Linux beats Windows for this bot:**
- Lower latency system calls (~10–20ms faster than Windows)
- No background OS processes stealing CPU
- Better Python async performance (asyncio + uvloop)
- Easy cron jobs, systemd auto-restart, screen/tmux sessions
- Free, stable, runs on VPS/cloud

### 🥈 OPTION 2: Windows 11 + WSL2 (Ubuntu)
- If you're on Windows, install WSL2 and run the bot inside Ubuntu
- Nearly identical performance to native Linux
- Best of both worlds if you prefer Windows desktop

### ❌ AVOID: macOS for production
- Fine for development, but don't run live bots on Mac
- Sleep/power management causes connection drops

### ☁️ RECOMMENDED VPS (for 24/7 uptime)
| Provider | Tier | Cost | Notes |
|----------|------|------|-------|
| Vultr | Regular Cloud | $6/mo | NYC or Miami datacenter |
| DigitalOcean | Basic Droplet | $6/mo | Easy setup |
| Hetzner | CX11 | $4/mo | EU-based, cheapest |
| AWS EC2 | t3.small | ~$15/mo | Most reliable |

> **Pick NYC or Miami** — closest to Polymarket's infrastructure

---

## 📦 SECTION 1 — FULL DOWNLOAD & SETUP ROADMAP

### STEP 1: Install Python 3.11+
```bash
# Ubuntu / WSL2
sudo apt update && sudo apt upgrade -y
sudo apt install python3.11 python3.11-venv python3-pip git curl wget -y

# Verify
python3.11 --version
```

### STEP 2: Create Project Directory
```bash
mkdir ~/polymarket-bot
cd ~/polymarket-bot
python3.11 -m venv venv
source venv/bin/activate
```

### STEP 3: Install All Required Python Packages
```bash
pip install --upgrade pip

# Core trading packages
pip install py-clob-client          # Polymarket CLOB API official client
pip install websockets               # WebSocket connections
pip install aiohttp                  # Async HTTP requests
pip install asyncio                  # Async framework (built-in, just install uvloop)
pip install uvloop                   # 2-3x faster event loop (Linux only)

# Data & analysis
pip install pandas numpy             # Data processing
pip install python-dotenv            # .env file management
pip install requests                 # HTTP requests

# Logging & monitoring
pip install loguru                   # Better logging
pip install colorama                 # Colored terminal output

# Optional: TradingView feed
pip install tradingview-ta           # TradingView technical analysis

# Save requirements
pip freeze > requirements.txt
```

### STEP 4: Install Git & Clone Setup
```bash
git init
echo "venv/" > .gitignore
echo ".env" >> .gitignore
echo "__pycache__/" >> .gitignore
echo "logs/" >> .gitignore
```

### STEP 5: Create .env File (YOUR KEYS GO HERE)
```bash
touch .env
nano .env
```
Paste this template:
```
POLYMARKET_API_KEY=your_api_key_here
POLYMARKET_API_SECRET=your_api_secret_here
POLYMARKET_API_PASSPHRASE=your_passphrase_here
POLYMARKET_PRIVATE_KEY=your_wallet_private_key_here
POLYGON_RPC_URL=https://polygon-rpc.com
PAPER_TRADING=true
INITIAL_CAPITAL=1000
MAX_TRADE_SIZE=5
```

---

## 🔑 SECTION — API SETUP (POLYMARKET)

### Creating Polymarket API Keys
1. Go to: https://polymarket.com
2. Connect your wallet (MetaMask on Polygon network)
3. Go to Profile → API Keys
4. Click **"Create New API Key"**
5. Save: `API Key`, `Secret`, `Passphrase`
6. Your **Private Key** = MetaMask wallet private key (Export from MetaMask → Account Details → Export Private Key)

### Polymarket CLOB Endpoints
```
REST API:    https://clob.polymarket.com
WebSocket:  wss://ws-subscriptions-clob.polymarket.com/ws/
Gamma API:  https://gamma-api.polymarket.com  (market discovery)
```

### Finding BTC 5-Min Market IDs
```bash
# Run this to find active BTC markets
python3 -c "
import requests
r = requests.get('https://gamma-api.polymarket.com/markets?tag=bitcoin&limit=20')
for m in r.json():
    print(m.get('question',''), '|', m.get('conditionId',''))
"
```

### MetaMask / Wallet Setup for Polygon
```bash
# Add Polygon network to MetaMask:
# Network Name: Polygon Mainnet
# RPC URL: https://polygon-rpc.com
# Chain ID: 137
# Currency: MATIC
# Explorer: https://polygonscan.com
```
> You'll need ~$5 MATIC for gas fees on first setup

---

## 📄 SECTION 3 — PAPER TRADING SETUP

Since Polymarket has **no official testnet**, paper trading works by:
- Connecting to **REAL live market data** (WebSocket)
- **Simulating** order fills based on real bid/ask spreads
- Tracking virtual P&L in a local JSON ledger
- Never sending actual orders to the CLOB

### How Paper Mode Works in the Bot:
```
PAPER_TRADING=true in .env
    ↓
Bot reads REAL market prices via WebSocket
    ↓
When signal triggers → Simulates fill at current mid-price
    ↓  
P&L tracked locally in paper_trades.json
    ↓
Run for 3–5 days, review results
    ↓
Switch PAPER_TRADING=false to go live
```

### Paper Trading Targets Before Going Live:
- ✅ Win rate > 52%
- ✅ Average profit per trade > 0.10%
- ✅ Zero crashes over 24-hour run
- ✅ Latency < 200ms per trade cycle
- ✅ No runaway loss scenarios

---

## 🤖 SECTION 5 — CLAUDEBOT (AI-POWERED SIGNALS)

The bot includes a ClaudeBot module that calls the Anthropic API to:
- Analyze current market conditions
- Classify market regime (trending/sideways)
- Suggest YES/NO bias based on recent price action
- Adjust confidence thresholds dynamically

### Setup:
```bash
pip install anthropic
```
Add to .env:
```
ANTHROPIC_API_KEY=your_claude_api_key
CLAUDE_ENABLED=true
```
Get API key: https://console.anthropic.com

---

## 🚀 RUNNING THE BOT

### Start Paper Trading:
```bash
cd ~/polymarket-bot
source venv/bin/activate
python3 btc_bot.py --mode paper
```

### Run in Background (Linux):
```bash
# Using screen (stays alive after SSH disconnect)
sudo apt install screen -y
screen -S polybot
python3 btc_bot.py --mode paper
# Detach: Ctrl+A then D
# Re-attach: screen -r polybot
```

### Check Logs:
```bash
tail -f logs/bot.log
tail -f logs/trades.log
```

### Switch to Live:
```bash
# Edit .env
PAPER_TRADING=false
# Then restart
python3 btc_bot.py --mode live
```

---

## ⚠️ RISK WARNINGS

| Rule | Value |
|------|-------|
| Max capital per trade | 0.5% |
| Daily loss limit | 2% |
| Min edge required | 0.3% |
| Position hold timeout | 4 minutes 30 sec max |
| Circuit breaker | 5 consecutive losses = pause 10 min |

---

## 📁 FINAL PROJECT STRUCTURE
```
polymarket-bot/
├── btc_bot.py           ← MAIN BOT SCRIPT
├── config.py            ← Configuration
├── .env                 ← YOUR KEYS (never commit this)
├── requirements.txt     ← Python packages
├── ROADMAP.md           ← This file
├── logs/
│   ├── bot.log
│   └── trades.log
└── data/
    ├── paper_trades.json
    └── market_cache.json
```
