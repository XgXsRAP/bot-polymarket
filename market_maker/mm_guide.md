# Polymarket Market Maker Bot — Complete Guide

## What Is Market Making (And Why Is It Different From Your Directional Bot)?

Your current bot is a **sniper**. It waits for a clear BTC momentum signal, enters one side of a trade, and exits at a profit target or stop loss. It makes money by being *right about direction*. The break-even win rate depends on your profit-to-loss ratio.

A market maker is a **shopkeeper**. Instead of betting on direction, you simultaneously offer to buy at one price and sell at a higher price. You profit from the *gap between your buy and sell prices* — the spread — regardless of which direction the market moves. Think of a currency exchange booth at an airport: they buy euros at $1.08 and sell them at $1.12. They don't care if the euro goes up or down tomorrow — they care that people walk up to the counter and trade.

Here's a concrete example. Imagine a BTC 5-minute prediction market where the YES contract has a fair value around $0.50 (50% chance BTC goes up). You post two orders:

**Your bid (buy order):** $0.48 — "I'll buy a YES share if anyone wants to sell to me at this price."
**Your ask (sell order):** $0.52 — "I'll sell a YES share if anyone wants to buy from me at this price."

Now two things happen over the next few minutes. Trader A comes along, bullish on BTC, and buys your YES share at $0.52. You just sold. Then Trader B comes along, bearish, and sells you a YES share at $0.48. You just bought. Net result: you own the same number of shares you started with (zero), but you pocketed $0.04 per share. That's one **round trip** — the atomic unit of market-making profit.

The critical question: "What if only one side fills?" That's **inventory risk** — the single biggest challenge in market making. If Trader A buys from you at $0.52 but nobody sells to you at $0.48, you're now *short* one YES share. If YES resolves to $1.00, you lose $0.48. This is why the bot has inventory management — it adjusts prices to encourage fills that reduce accumulated inventory.

---

## How the Bot Works — Architecture

```
Binance/Kraken WS → BTCPriceFeed → FairValueEngine → QuoteEngine
                                                          ↓
Polymarket REST    → PolymarketFeed ──────────────→ Quote prices + sizes
                                                          ↓
                                    MMRiskManager → OrderManager (Paper or Live)
                                         ↓                    ↓
                                    Inventory tracking    Post/cancel/check fills
                                         ↓
                                    Adjust skew → feed back into QuoteEngine
```

### The Core Loop (runs every 500ms)

The bot repeats this cycle twice per second:

**Step 1 — Estimate fair value.** Using BTC price momentum (dampened compared to the directional bot), combined with the current market mid-price. This gives us the center point for our quotes.

**Step 2 — Generate quotes.** The QuoteEngine takes fair value, current inventory, and volatility, and produces bid/ask prices for both YES and NO sides. The key innovation here is **inventory skew** — if we're holding too many YES shares, we make our ask slightly cheaper to attract buyers and our bid slightly worse to discourage sellers. This nudges the market into reducing our inventory.

**Step 3 — Cancel old quotes and post new ones.** Because the market moves every second, our old quotes may be stale. We cancel them and post fresh ones at the new calculated prices.

**Step 4 — Check for fills.** Did anyone trade against our quotes since last cycle? If so, update inventory and P&L.

**Step 5 — Risk checks.** Are we within inventory limits? Have we hit our daily loss limit? Are we getting adversely selected (fills that immediately move against us)?

---

## The Three Key Concepts You Must Understand

### 1. The Spread (Where Your Profit Comes From)

The spread is the distance between your bid and ask. If your bid is $0.48 and your ask is $0.52, the spread is $0.04 (4 cents). Each round trip (one buy fill + one sell fill) captures approximately this amount per share.

**Tight spread (2¢):** More fills, but more adverse selection risk. Informed traders will pick off your quotes when they know the price is about to move. You'll get lots of small wins punctuated by larger losses.

**Wide spread (8¢):** Fewer fills, but safer. Other market makers will undercut you, so you might sit there with no activity. Your capital earns nothing.

**Sweet spot for Polymarket BTC markets: 3–5¢.** The bot starts at 4¢ and widens to up to 10¢ when BTC volatility spikes. This is controlled by `base_spread_pct` (0.04) and `max_spread_pct` (0.10) in the config.

**Mental model:** Think of the spread as your "insurance premium" against the market moving before both sides fill. Higher volatility = higher premium needed. The `volatility_spread_multiplier` parameter controls how aggressively the spread widens when BTC is moving fast.

### 2. Inventory Skew (How You Control Risk)

This is the most important concept in market making. Your goal is to keep your net position near zero — you're a middleman, not a speculator. But fills don't come in perfectly matched pairs. Sometimes you'll buy three times before anyone sells to you, leaving you with +60 shares of YES. Now you're involuntarily *long YES* and exposed to directional risk.

**Inventory skew** is the solution. When you're long YES, you shift *all* your quotes slightly downward:

```
Without skew (inventory = 0):     Bid $0.48  /  Ask $0.52
With skew (inventory = +50 YES):  Bid $0.47  /  Ask $0.51
```

Why? The lower ask ($0.51 instead of $0.52) makes it cheaper for someone to buy from you — encouraging them to take those YES shares off your hands. The lower bid ($0.47 instead of $0.48) makes it less attractive for someone to sell to you — discouraging adding more YES shares to your inventory.

The formula: `skew = net_inventory × inventory_skew_factor`. The default `inventory_skew_factor` is 0.002, meaning each share of net inventory shifts your quotes by 0.2 cents. At +50 shares, that's a 10-cent shift — very aggressive.

When inventory exceeds 75% of the max (the `inventory_panic_threshold`), the skew doubles — the bot is now pricing very aggressively to shed inventory, accepting worse fills to reduce risk.

### 3. Adverse Selection (Your Biggest Enemy)

**Adverse selection** is what happens when someone who knows more than you trades against your quotes. Imagine you're quoting YES at bid $0.48 / ask $0.52. A whale who has better BTC price feeds than you sees that BTC just dropped 0.5% in 200ms. They instantly sell YES to you at $0.48 (your bid). By the time you process the fill, the fair value of YES has dropped to $0.44, and you just bought at $0.48 — a 4-cent loss before you even had time to react.

This is the fundamental tension in market making: **you want your quotes to be tight (more fills, more profit per round trip) but the tighter they are, the more vulnerable you are to informed traders picking you off.**

The bot mitigates adverse selection three ways. First, **volatility-based spread widening** — when BTC is moving fast, the spread widens, giving you more buffer against sudden moves. Second, **the adverse fill detector** — if 10 fills in a row lose money (the market moved against you immediately after the fill), the bot pauses for 5 minutes. This means you were getting systematically picked off and need to wait for conditions to calm down. Third, **the fair value model** — by incorporating BTC momentum into your fair value estimate, your quotes naturally shift in the direction the market is moving, reducing the chance of being caught on the wrong side.

---

## Running the Bot

### Paper trading (start here)

```bash
cd ~/polymarket-bot
source venv/bin/activate

# Terminal 1: the market maker
python market_maker.py --mode paper

# Terminal 2: watch the state file (optional)
watch -n 1 cat data/mm_state.json
```

The bot will connect to Binance (or Kraken), discover active BTC markets, and start posting simulated quotes. You'll see log output like:

```
🏪 POLYMARKET MARKET MAKER STARTING
   Mode: PAPER
   Capital: $1000.00
   Base spread: 4.0¢
   Quote size: 20 shares
✅ BTC feed connected (Binance)
📡 Discovered 2 new BTC markets (total: 2)
🟢 FILL | YES BUY 20 shares @ $0.4800 | Net inventory: +20.0
🔴 FILL | YES SELL 20 shares @ $0.5200 | Net inventory: 0.0 | Spread earned: $0.0040
🏪 MM STATUS | BTC: $87,234 | Markets: 2 | Fills: 14 | Spread P&L: +$0.0320
```

### Configuration tuning

All the important knobs are in the `MMConfig` dataclass at the top of `market_maker.py`. Each parameter has extensive documentation in the code comments. The most impactful ones to experiment with:

**`base_spread_pct` (default: 0.04):** Start here. Run the bot for a day at 0.04, then try 0.03 and 0.05. Track fills per hour and P&L per fill. You're looking for the tightest spread that doesn't produce net losses from adverse selection.

**`quote_size_shares` (default: 20):** How much capital per quote. Larger = more spread captured per fill, but more inventory risk if only one side fills.

**`inventory_skew_factor` (default: 0.002):** How aggressively to shift quotes when holding inventory. If you're consistently ending up with large positions, increase this. If your quotes are so skewed nobody trades against them, decrease it.

**`volatility_spread_multiplier` (default: 5.0):** How much the spread widens per unit of BTC volatility. Higher = more defensive. Lower = more aggressive.

---

## How This Complements Your Directional Bot

The market maker and directional bot can run simultaneously on different markets (or even the same markets, with careful position management). They're complementary strategies:

**Directional bot** excels when BTC has clear momentum and the market is mispricing the probability. It makes few trades but targets 2.5% per trade.

**Market maker** excels when the market is choppy/sideways — exactly the conditions where the directional bot stops trading. It makes many small trades (1–4¢ each) and profits from *volume*, not direction.

Together, they cover more market conditions: the directional bot captures trending moves while the market maker earns during the flat periods in between.

---

## Key Metrics to Watch

**Fills per hour:** A healthy market maker should get 5–20 fills per hour on an active market. Fewer than 5 means your spread is too wide. More than 30 might mean your spread is too tight (you're being adversely selected).

**Spread P&L vs Inventory P&L:** The bot tracks both. "Spread P&L" is profit from completed round trips — this should always be positive if your spread is reasonable. "Inventory P&L" is the mark-to-market value of shares you're holding — this fluctuates. If inventory P&L is consistently negative and larger than spread P&L, you're being adversely selected.

**Net inventory:** Should oscillate around zero. If it consistently drifts in one direction, your fair value estimate may be biased. Increase `inventory_skew_factor` or improve the fair value model.

**Quote utilization:** What fraction of your posted quotes actually get filled? Below 5% means your spread is too wide. Above 50% means it's probably too tight.

---

## What To Build Next

Once the market maker is running profitably in paper mode, the highest-value improvements are:

**WebSocket fill detection** — The paper version polls for fills. In live mode, you want to subscribe to the Polymarket CLOB WebSocket for real-time fill notifications. This eliminates the 500ms delay between a fill and your reaction.

**Multi-level quoting** — Instead of one bid and one ask, post 2–3 levels on each side at different prices and sizes. This captures more volume and provides better liquidity. Example: Bid 20 shares at $0.48, Bid 30 shares at $0.46, Ask 20 shares at $0.52, Ask 30 shares at $0.54.

**Cross-market hedging** — If you accumulate YES inventory on Market A (BTC up in 5 min?), and Market B (BTC up in 15 min?) is also active, you could hedge by buying NO on Market B. This reduces directional exposure while keeping spread-earning capacity.

**Machine learning fair value** — Replace the simple momentum-based fair value with a trained model that uses order flow imbalance (are there more bids or asks appearing?) to predict short-term price direction. This is the single biggest source of alpha in professional market making.
