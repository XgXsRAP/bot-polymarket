"""
Market Maker Dashboard — two modes:

  python mm_dashboard.py           → Plain terminal UI (no extra deps)
  python mm_dashboard.py --web     → aiohttp web server on port 8889

Both read from data/paper_mm_state.json written by the paper trader.
Run the dashboard in a second terminal while the bot runs.

Prices are displayed in cents (same as Polymarket UI).
Underlying bot values remain in decimal (0.0–1.0) — display only.
"""

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path

STATE_FILE = str(Path(__file__).resolve().parent / "data" / "paper_mm_state.json")
REFRESH_RATE = 1.0   # seconds
WIDTH = 80           # terminal columns


# ── ANSI colors ───────────────────────────────────────────────────────────────

R  = "\033[0m"       # reset
G  = "\033[92m"      # bright green
GD = "\033[32m"      # dim green
RD = "\033[91m"      # bright red
CY = "\033[96m"      # cyan
YL = "\033[93m"      # yellow
DM = "\033[2m"       # dim
BL = "\033[1m"       # bold
WH = "\033[97m"      # bright white


# ── Shared helpers ─────────────────────────────────────────────────────────────

def load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _since(ts: float) -> str:
    if not ts:
        return "never"
    age = time.time() - ts
    if age < 60:
        return f"{age:.0f}s ago"
    return f"{age/60:.1f}m ago"


def _cents(v: float) -> str:
    """Convert 0.0–1.0 probability to Polymarket-style cents string."""
    return f"{round(v * 100):.0f}c"


def _pnl(v: float) -> str:
    sign = "+" if v >= 0 else ""
    return f"{sign}${v:.2f}"


def _bar(pct: float, width: int = 20) -> str:
    filled = max(0, min(width, round(pct / 100 * width)))
    return "#" * filled + "." * (width - filled)


def _expiry_bar(secs: float, total: float = 300.0, width: int = 20) -> str:
    ratio = max(0.0, min(1.0, secs / total))
    filled = round(ratio * width)
    return "#" * filled + "." * (width - filled)


def _arrow(v: float) -> str:
    return "^" if v > 0.1 else ("v" if v < -0.1 else "-")


# ── Terminal renderer ──────────────────────────────────────────────────────────

SEP = "=" * WIDTH
DIV = "-" * WIDTH


def _sig_color(v: float) -> str:
    if v > 0.1:  return G
    if v < -0.1: return RD
    return DM

def _pnl_color(v: float) -> str:
    return G if v >= 0 else RD

def _expiry_color(secs: float) -> str:
    if secs < 30:  return RD
    if secs < 60:  return YL
    return G


def render(s: dict) -> str:
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = []

    # ── Header ────────────────────────────────────────────────────────────────
    title = "  BTC 5-MIN MARKET MAKER"
    sep_c  = f"{GD}{SEP}{R}"
    div_c  = f"{GD}{DIV}{R}"
    padding = WIDTH - len(title) - len(now_str) - 2
    lines.append(sep_c)
    lines.append(f"{BL}{G}{title}{R}{' ' * max(1, padding)}{DM}{now_str}{R}  ")
    lines.append(sep_c)

    # ── BTC price ─────────────────────────────────────────────────────────────
    btc = s.get("btc_price", 0)
    ch1 = s.get("btc_change_1m", 0) * 100
    ch5 = s.get("btc_change_5m", 0) * 100
    vol = s.get("btc_volatility_1m", 0)
    ch1_col = G if ch1 >= 0 else RD
    ch5_col = G if ch5 >= 0 else RD
    ch1_str = f"{ch1_col}({'+' if ch1 >= 0 else ''}{ch1:.3f}% 1m){R}"
    ch5_str = f"{ch5_col}({'+' if ch5 >= 0 else ''}{ch5:.3f}% 5m){R}"
    lines.append(f"  {BL}BTC{R}  {CY}${btc:,.2f}{R}  {ch1_str}  {ch5_str}   {DM}vol:{vol:.5f}{R}")
    lines.append(div_c)

    # ── Active market + expiry ─────────────────────────────────────────────────
    mkt_id = s.get("market_id") or "Waiting for market..."
    expiry = s.get("seconds_to_expiry", 300)
    mins = int(expiry // 60)
    secs = int(expiry % 60)
    eb = _expiry_bar(expiry, 300, 24)
    ecol = _expiry_color(expiry)
    lines.append(f"  {BL}MARKET{R}  {DM}{mkt_id[:62]}{R}")
    lines.append(f"  {BL}EXPIRY{R}  {ecol}[{eb}]{R}  {ecol}{mins}m {secs:02d}s remaining{R}")
    lines.append(div_c)

    # ── Signals + Quotes (two columns) ────────────────────────────────────────
    cvd  = s.get("cvd_signal",   0)
    fund = s.get("funding_signal", 0)
    liq  = s.get("liq_signal",   0)
    oi   = s.get("oi_signal",    0)

    yes_bid = s.get("current_yes_bid", 0)
    yes_ask = s.get("current_yes_ask", 0)
    fv      = s.get("current_fair_value", 0.5)
    spread  = s.get("current_spread", 0)
    mbb = s.get("market_best_bid", 0)
    mba = s.get("market_best_ask", 0)

    lines.append(f"  {BL}{'SIGNALS':<36}QUOTES (cents — Polymarket format){R}")
    lines.append(
        f"  CVD      {_sig_color(cvd)}{cvd:+.3f}  {_arrow(cvd)}{R}  buy/sell flow  "
        f"  {BL}YES BID{R}  {G}{_cents(yes_bid):>4s}{R}  {DM}(mkt {_cents(mbb):>4s}){R}  <- posting"
    )
    lines.append(
        f"  Funding  {_sig_color(fund)}{fund:+.3f}  {_arrow(fund)}{R}  crowd pos      "
        f"  {BL}YES ASK{R}  {RD}{_cents(yes_ask):>4s}{R}  {DM}(mkt {_cents(mba):>4s}){R}  <- posting"
    )
    lines.append(
        f"  Liq      {_sig_color(liq)}{liq:+.3f}  {_arrow(liq)}{R}  liquidations   "
        f"  Fair Val {CY}{_cents(fv):>4s}{R}"
    )
    lines.append(
        f"  OI       {_sig_color(oi)}{oi:+.3f}  {_arrow(oi)}{R}  new money      "
        f"  Spread   {YL}{_cents(spread):>4s}{R}"
    )
    lines.append(div_c)

    # ── Confidence ────────────────────────────────────────────────────────────
    conf   = s.get("current_confidence", 0)
    tier   = s.get("confidence_tier", "PAUSED")
    reason = s.get("confidence_reason", "no data")
    cb = _bar(conf, 24)
    tier_col = G if tier == "NORMAL" else (YL if tier in ("CAUTIOUS", "WIDE") else RD)
    lines.append(
        f"  {BL}CONFIDENCE{R}  {G}[{cb}]{R}  {conf:.0f}%  "
        f"{tier_col}[{tier}]{R}  {DM}{reason}{R}"
    )
    lines.append(div_c)

    # ── P&L ───────────────────────────────────────────────────────────────────
    rpnl  = s.get("realized_pnl", 0)
    upnl  = s.get("unrealized_pnl", 0)
    tpnl  = rpnl + upnl
    fills = s.get("total_fills", 0)
    trips = s.get("round_trips", 0)
    wins  = s.get("winning_trips", 0)
    wr    = (wins / trips * 100) if trips > 0 else 0
    inv   = s.get("net_inventory", 0)
    dd    = s.get("max_drawdown", 0)
    peak  = s.get("peak_capital", 0)
    updated = _since(s.get("last_update", 0))

    def cpnl(v):
        c = G if v >= 0 else RD
        return f"{c}{_pnl(v)}{R}"

    lines.append(
        f"  {BL}P&L{R}   {cpnl(tpnl):>10s}  |  Realized {cpnl(rpnl):>10s}  |  Unrealized {cpnl(upnl):>10s}"
    )
    lines.append(
        f"  Fills {WH}{fills:>7d}{R}       |  Trips  {WH}{trips:<7d}{R}        |  Win Rate   {WH}{wr:.0f}%{R}"
    )
    inv_col = RD if abs(inv) > 10 else (YL if abs(inv) > 5 else G)
    lines.append(
        f"  Inv  {inv_col}{inv:>+7.0f}{R}        |  Max DD {RD}${dd:<8.2f}{R}        |  Peak {G}${peak:.2f}{R}"
    )
    lines.append(f"  {DM}Updated: {updated}{R}")
    lines.append(sep_c)

    return "\n".join(lines)


def run_terminal():
    """Plain terminal dashboard — clears screen each cycle, no external deps."""
    CLEAR = "\033[H\033[J"
    try:
        while True:
            s = load_state()
            sys.stdout.write(CLEAR)
            if not s:
                sys.stdout.write(
                    f"{GD}{SEP}{R}\n"
                    f"  {BL}{G}BTC 5-MIN MARKET MAKER{R}\n"
                    f"{GD}{SEP}{R}\n"
                    f"  {YL}Waiting for bot state...{R}\n\n"
                    f"  {DM}Run:  source venv/bin/activate && python mm_enhanced_1.py --paper{R}\n"
                    f"{GD}{SEP}{R}\n"
                )
            else:
                sys.stdout.write(render(s) + "\n")
            sys.stdout.flush()
            time.sleep(REFRESH_RATE)
    except KeyboardInterrupt:
        print("\nDashboard stopped.")


# ── Web dashboard ──────────────────────────────────────────────────────────────

WEB_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>BTC Market Maker</title>
<style>
  :root {
    --bg: #f4f6f8;
    --panel: #ffffff;
    --text: #16202a;
    --muted: #6b7785;
    --line: #d8e0e7;
    --green: #1a9c5f;
    --red: #d24d57;
    --yellow: #c58a12;
    --blue: #276ef1;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    padding: 20px;
  }
  .shell { max-width: 1080px; margin: 0 auto; }
  .topbar {
    display: flex;
    justify-content: space-between;
    gap: 16px;
    align-items: flex-end;
    margin-bottom: 18px;
  }
  .title {
    font-size: 28px;
    font-weight: 700;
    margin-bottom: 4px;
  }
  .subtitle, .refresh-note { color: var(--muted); font-size: 14px; }
  .hero {
    background: linear-gradient(135deg, #ffffff 0%, #eef4fb 100%);
    border: 1px solid var(--line);
    border-radius: 18px;
    padding: 20px;
    margin-bottom: 16px;
    box-shadow: 0 8px 24px rgba(16, 24, 40, 0.06);
  }
  .hero-grid {
    display: grid;
    grid-template-columns: 1.4fr 1fr 1fr 1fr;
    gap: 14px;
    align-items: end;
  }
  .eyebrow {
    color: var(--muted);
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    margin-bottom: 6px;
  }
  .price {
    font-size: 42px;
    font-weight: 800;
    line-height: 1;
  }
  .metric {
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: 14px;
    padding: 14px;
  }
  .metric-value {
    font-size: 24px;
    font-weight: 700;
  }
  .grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 16px;
  }
  .card {
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: 18px;
    padding: 18px;
    box-shadow: 0 8px 24px rgba(16, 24, 40, 0.04);
  }
  .card h2 {
    margin: 0 0 14px 0;
    font-size: 16px;
  }
  .row {
    display: flex;
    justify-content: space-between;
    gap: 16px;
    padding: 10px 0;
    border-bottom: 1px solid var(--line);
    align-items: center;
  }
  .row:last-child { border-bottom: none; padding-bottom: 0; }
  .label { color: var(--muted); }
  .value { text-align: right; font-weight: 600; }
  .muted { color: var(--muted); }
  .green { color: var(--green); }
  .red { color: var(--red); }
  .yellow { color: var(--yellow); }
  .blue { color: var(--blue); }
  .market-id {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 12px;
    line-height: 1.5;
    color: var(--muted);
    word-break: break-all;
    margin-bottom: 14px;
  }
  .progress {
    height: 10px;
    background: #e8edf2;
    border-radius: 999px;
    overflow: hidden;
    margin-bottom: 8px;
  }
  .progress > div {
    height: 100%;
    border-radius: 999px;
  }
  .mini-grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 12px;
  }
  .pill {
    display: inline-flex;
    align-items: center;
    padding: 4px 10px;
    border-radius: 999px;
    background: #eef4fb;
    color: var(--blue);
    font-size: 12px;
    font-weight: 700;
  }
  .empty {
    padding: 32px 20px;
    text-align: center;
    color: var(--muted);
    background: var(--panel);
    border: 1px dashed var(--line);
    border-radius: 18px;
  }
  @media (max-width: 900px) {
    .hero-grid, .grid { grid-template-columns: 1fr; }
    .topbar { flex-direction: column; align-items: flex-start; }
    .price { font-size: 34px; }
  }
</style>
</head>
<body>
<div class="shell">
  <div class="topbar">
    <div>
      <div class="title">BTC Market Maker</div>
      <div class="subtitle">Simple live view for the paper market maker</div>
    </div>
    <div class="refresh-note">Auto-refreshing every second</div>
  </div>
  <div id="app" class="empty">Loading dashboard...</div>
</div>
<script>
function asNumber(v, fallback = 0) {
  return Number.isFinite(Number(v)) ? Number(v) : fallback;
}

function formatMoney(v) {
  const n = asNumber(v);
  const cls = n >= 0 ? 'green' : 'red';
  const sign = n >= 0 ? '+' : '-';
  return `<span class="${cls}">${sign}$${Math.abs(n).toFixed(2)}</span>`;
}

function formatPercentFromDecimal(v) {
  const n = asNumber(v) * 100;
  const cls = n >= 0 ? 'green' : 'red';
  const sign = n >= 0 ? '+' : '';
  return `<span class="${cls}">${sign}${n.toFixed(3)}%</span>`;
}

function formatSignal(v) {
  const n = asNumber(v);
  const cls = n > 0.1 ? 'green' : n < -0.1 ? 'red' : 'muted';
  return `<span class="${cls}">${n >= 0 ? '+' : ''}${n.toFixed(3)}</span>`;
}

function formatCents(v) {
  return `${Math.round(asNumber(v) * 100)}c`;
}

function formatTime(ts) {
  if (!ts) return 'never';
  const age = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (age < 60) return `${age}s ago`;
  return `${(age / 60).toFixed(1)}m ago`;
}

function confidenceColor(conf) {
  if (conf >= 80) return 'var(--green)';
  if (conf >= 60) return 'var(--yellow)';
  if (conf >= 40) return '#ea8c2d';
  return 'var(--red)';
}

function expiryColor(exp) {
  if (exp < 30) return 'var(--red)';
  if (exp < 60) return 'var(--yellow)';
  return 'var(--green)';
}

async function update() {
  try {
    const response = await fetch('/state');
    const s = await response.json();

    if (!s || !Object.keys(s).length) {
      document.getElementById('app').className = 'empty';
      document.getElementById('app').innerHTML = 'Waiting for bot state...';
      return;
    }

    const btcPrice = asNumber(s.btc_price);
    const exp = asNumber(s.seconds_to_expiry, 300);
    const expPct = Math.max(0, Math.min(100, exp / 300 * 100));
    const mins = Math.floor(exp / 60);
    const secs = Math.floor(exp % 60).toString().padStart(2, '0');
    const conf = asNumber(s.current_confidence);
    const rpnl = asNumber(s.realized_pnl);
    const upnl = asNumber(s.unrealized_pnl);
    const totalPnl = rpnl + upnl;
    const trips = asNumber(s.round_trips);
    const wins = asNumber(s.winning_trips);
    const winRate = trips > 0 ? ((wins / trips) * 100).toFixed(0) : '0';
    const inventory = asNumber(s.net_inventory);

    document.getElementById('app').className = '';
    document.getElementById('app').innerHTML = `
      <section class="hero">
        <div class="hero-grid">
          <div>
            <div class="eyebrow">BTC Price</div>
            <div class="price">$${btcPrice.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</div>
            <div class="muted" style="margin-top:8px">
              1m ${formatPercentFromDecimal(s.btc_change_1m)}
              &nbsp;&nbsp; 5m ${formatPercentFromDecimal(s.btc_change_5m)}
            </div>
          </div>
          <div class="metric">
            <div class="eyebrow">Confidence</div>
            <div class="metric-value">${conf.toFixed(0)}%</div>
            <div class="pill" style="margin-top:8px">${s.confidence_tier || 'PAUSED'}</div>
          </div>
          <div class="metric">
            <div class="eyebrow">Total P&L</div>
            <div class="metric-value">${formatMoney(totalPnl)}</div>
            <div class="muted" style="margin-top:8px">Updated ${formatTime(s.last_update)}</div>
          </div>
          <div class="metric">
            <div class="eyebrow">Inventory</div>
            <div class="metric-value ${inventory === 0 ? 'blue' : inventory > 0 ? 'green' : 'red'}">${inventory.toFixed(0)} sh</div>
            <div class="muted" style="margin-top:8px">Fills ${asNumber(s.total_fills).toFixed(0)}</div>
          </div>
        </div>
      </section>

      <section class="grid">
        <div class="card">
          <h2>Active Market</h2>
          <div class="market-id">${s.market_id || 'Waiting for market...'}</div>
          <div class="eyebrow">Time To Expiry</div>
          <div class="progress">
            <div style="width:${expPct}%; background:${expiryColor(exp)}"></div>
          </div>
          <div class="value" style="text-align:left; margin-bottom:16px">${mins}m ${secs}s remaining</div>
          <div class="eyebrow">Reason</div>
          <div class="muted">${s.confidence_reason || 'No reason available'}</div>
        </div>

        <div class="card">
          <h2>Quotes</h2>
          <div class="row"><div class="label">YES Bid</div><div class="value green">${formatCents(s.current_yes_bid)}</div></div>
          <div class="row"><div class="label">YES Ask</div><div class="value red">${formatCents(s.current_yes_ask)}</div></div>
          <div class="row"><div class="label">Fair Value</div><div class="value">${formatCents(s.current_fair_value)}</div></div>
          <div class="row"><div class="label">Spread</div><div class="value yellow">${formatCents(s.current_spread)}</div></div>
          <div class="row"><div class="label">Market Best</div><div class="value muted">${formatCents(s.market_best_bid)} / ${formatCents(s.market_best_ask)}</div></div>
        </div>

        <div class="card">
          <h2>Signals</h2>
          <div class="row"><div class="label">CVD</div><div class="value">${formatSignal(s.cvd_signal)}</div></div>
          <div class="row"><div class="label">Funding</div><div class="value">${formatSignal(s.funding_signal)}</div></div>
          <div class="row"><div class="label">Liquidation</div><div class="value">${formatSignal(s.liq_signal)}</div></div>
          <div class="row"><div class="label">Open Interest</div><div class="value">${formatSignal(s.oi_signal)}</div></div>
          <div class="row"><div class="label">1m Volatility</div><div class="value muted">${asNumber(s.btc_volatility_1m).toFixed(5)}</div></div>
        </div>

        <div class="card">
          <h2>Performance</h2>
          <div class="mini-grid" style="margin-bottom:14px">
            <div class="metric">
              <div class="eyebrow">Realized</div>
              <div class="metric-value">${formatMoney(rpnl)}</div>
            </div>
            <div class="metric">
              <div class="eyebrow">Unrealized</div>
              <div class="metric-value">${formatMoney(upnl)}</div>
            </div>
          </div>
          <div class="row"><div class="label">Round Trips</div><div class="value">${trips.toFixed(0)}</div></div>
          <div class="row"><div class="label">Win Rate</div><div class="value">${winRate}%</div></div>
          <div class="row"><div class="label">Max Drawdown</div><div class="value red">$${asNumber(s.max_drawdown).toFixed(2)}</div></div>
          <div class="row"><div class="label">Peak Capital</div><div class="value">$${asNumber(s.peak_capital).toFixed(2)}</div></div>
        </div>
      </section>
    `;

    const bar = document.querySelector('.progress > div');
    if (bar) {
      bar.style.background = expiryColor(exp);
    }

    const confidenceBox = document.querySelector('.hero .metric:nth-child(2)');
    if (confidenceBox) {
      confidenceBox.style.borderColor = confidenceColor(conf);
    }
  } catch (error) {
    document.getElementById('app').className = 'empty';
    document.getElementById('app').innerHTML = 'Waiting for bot state...';
  }
}

update();
setInterval(update, 1000);
</script>
</body>
</html>"""


async def run_web(host: str = "0.0.0.0", port: int = 8889):
    try:
        from aiohttp import web
    except ImportError:
        print("aiohttp not installed. Run: venv/bin/pip install aiohttp")
        return

    async def handle_root(request):
        return web.Response(text=WEB_HTML, content_type="text/html")

    async def handle_state(request):
        s = load_state()
        return web.json_response(s)

    app = web.Application()
    app.router.add_get("/", handle_root)
    app.router.add_get("/state", handle_state)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    print(f"Web dashboard: http://localhost:{port}")
    print("Press Ctrl+C to stop.")
    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await runner.cleanup()


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Market Maker Dashboard")
    parser.add_argument("--web", action="store_true",
                        help="Run web dashboard on port 8889 instead of terminal UI")
    parser.add_argument("--port", type=int, default=8889)
    args = parser.parse_args()

    if args.web:
        asyncio.run(run_web(port=args.port))
    else:
        run_terminal()


if __name__ == "__main__":
    main()
