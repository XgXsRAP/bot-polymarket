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
<title>BTC Market Maker</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #0d1117; color: #e6edf3; font-family: 'Courier New', monospace; font-size: 13px; padding: 16px; }
  h1 { color: #e6edf3; border-bottom: 1px solid #30363d; padding-bottom: 8px; margin-bottom: 16px; font-size: 15px; letter-spacing: 1px; }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-bottom: 8px; }
  .card { background: #161b22; border: 1px solid #21262d; padding: 10px 14px; }
  .card h2 { color: #8b949e; font-size: 10px; text-transform: uppercase; letter-spacing: 2px; margin-bottom: 8px; border-bottom: 1px solid #21262d; padding-bottom: 4px; }
  .big { font-size: 24px; font-weight: bold; color: #f0f6fc; }
  .green { color: #3fb950; } .red { color: #f85149; } .yellow { color: #d29922; } .dim { color: #8b949e; }
  .row { display: flex; justify-content: space-between; padding: 3px 0; border-bottom: 1px solid #21262d; font-size: 12px; }
  .row:last-child { border: none; }
  .full { grid-column: 1 / -1; }
  .bar-bg { background: #21262d; height: 8px; margin: 6px 0; }
  .bar-fg { height: 8px; }
  .expiry-bg { background: #21262d; height: 14px; position: relative; margin: 6px 0; }
  .expiry-fg { height: 14px; }
  .expiry-label { position: absolute; top: 0; left: 50%; transform: translateX(-50%); line-height: 14px; font-size: 11px; }
  .stat-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 4px; }
  .stat { }
  .stat .label { color: #8b949e; font-size: 10px; margin-bottom: 2px; }
  .stat .val { font-size: 16px; font-weight: bold; }
  pre { font-family: inherit; font-size: 12px; color: #8b949e; }
</style>
</head>
<body>
<h1>BTC 5-MIN MARKET MAKER</h1>
<div id="app"><pre>Loading...</pre></div>
<script>
async function update() {
  try {
    const r = await fetch('/state');
    const s = await r.json();
    const f2 = v => (v||0).toFixed(2);
    const f3 = v => (v||0).toFixed(3);
    const cents = v => Math.round((v||0)*100) + 'c';
    const pnl = v => v >= 0
      ? '<span class="green">+$' + f2(v) + '</span>'
      : '<span class="red">-$' + f2(Math.abs(v)) + '</span>';
    const pct = v => {
      const x = (v||0)*100;
      return x >= 0
        ? '<span class="green">+' + x.toFixed(3) + '%</span>'
        : '<span class="red">'  + x.toFixed(3) + '%</span>';
    };
    const arrow = v => v > 0.1 ? '&#x25B2;' : v < -0.1 ? '&#x25BC;' : '&mdash;';
    const sigCls = v => v > 0.1 ? 'green' : v < -0.1 ? 'red' : 'dim';

    const exp = s.seconds_to_expiry || 300;
    const expPct = Math.max(0, Math.min(100, exp / 300 * 100));
    const expColor = exp < 30 ? '#f85149' : exp < 60 ? '#d29922' : '#3fb950';
    const mins = Math.floor(exp/60), secs = Math.floor(exp%60);

    const conf = s.current_confidence || 0;
    const tier = s.confidence_tier || 'PAUSED';
    const confColor = conf>=80 ? '#3fb950' : conf>=60 ? '#d29922' : conf>=40 ? '#f0883e' : '#f85149';

    const trips = s.round_trips || 0;
    const wins  = s.winning_trips || 0;
    const wr    = trips > 0 ? (wins/trips*100).toFixed(0) : '0';
    const rpnl  = s.realized_pnl   || 0;
    const upnl  = s.unrealized_pnl || 0;

    document.getElementById('app').innerHTML = `
    <div class="grid">

      <div class="card full">
        <h2>BTC Price</h2>
        <span class="big">$${(s.btc_price||0).toLocaleString('en',{minimumFractionDigits:2,maximumFractionDigits:2})}</span>
        &nbsp; ${pct(s.btc_change_1m)} 1m &nbsp; ${pct(s.btc_change_5m)} 5m
        &nbsp; <span class="dim">vol ${f3(s.btc_volatility_1m)}</span>
      </div>

      <div class="card full">
        <h2>Active Market</h2>
        <div class="dim" style="margin-bottom:4px;font-size:12px">${s.market_id || 'Waiting for market...'}</div>
        <div class="expiry-bg">
          <div class="expiry-fg" style="width:${expPct}%;background:${expColor}"></div>
          <div class="expiry-label" style="color:#e6edf3">${mins}m ${secs.toString().padStart(2,'0')}s</div>
        </div>
      </div>

      <div class="card">
        <h2>Signals</h2>
        <div class="row"><span>CVD</span><span class="${sigCls(s.cvd_signal)}">${f3(s.cvd_signal)} ${arrow(s.cvd_signal)}</span></div>
        <div class="row"><span>Funding</span><span class="${sigCls(s.funding_signal)}">${f3(s.funding_signal)} ${arrow(s.funding_signal)}</span></div>
        <div class="row"><span>Liq</span><span class="${sigCls(s.liq_signal)}">${f3(s.liq_signal)} ${arrow(s.liq_signal)}</span></div>
        <div class="row"><span>OI</span><span class="${sigCls(s.oi_signal)}">${f3(s.oi_signal)} ${arrow(s.oi_signal)}</span></div>
      </div>

      <div class="card">
        <h2>Quotes &mdash; cents (Polymarket format)</h2>
        <div class="row"><span>YES BID</span><span class="green">${cents(s.current_yes_bid)}</span><span class="dim">mkt ${cents(s.market_best_bid)}</span></div>
        <div class="row"><span>YES ASK</span><span class="red">${cents(s.current_yes_ask)}</span><span class="dim">mkt ${cents(s.market_best_ask)}</span></div>
        <div class="row"><span>Fair Value</span><span>${cents(s.current_fair_value)}</span></div>
        <div class="row"><span>Spread</span><span class="yellow">${cents(s.current_spread)}</span></div>
      </div>

      <div class="card full">
        <h2>Confidence &mdash; ${conf.toFixed(0)}% [${tier}]</h2>
        <div class="bar-bg"><div class="bar-fg" style="width:${conf}%;background:${confColor}"></div></div>
        <span class="dim" style="font-size:11px">${s.confidence_reason || ''}</span>
      </div>

      <div class="card full">
        <h2>P&L</h2>
        <div class="stat-grid" style="margin-bottom:8px">
          <div class="stat"><div class="label">Total P&L</div><div class="val">${pnl(rpnl+upnl)}</div></div>
          <div class="stat"><div class="label">Realized</div><div class="val">${pnl(rpnl)}</div></div>
          <div class="stat"><div class="label">Unrealized</div><div class="val">${pnl(upnl)}</div></div>
        </div>
        <div class="row"><span class="dim">Fills</span><span>${s.total_fills||0}</span>
          <span class="dim">Round Trips</span><span>${trips}</span>
          <span class="dim">Win Rate</span><span>${wr}%</span></div>
        <div class="row"><span class="dim">Inventory</span><span>${(s.net_inventory||0).toFixed(0)} sh</span>
          <span class="dim">Max DD</span><span class="red">$${f2(s.max_drawdown)}</span>
          <span class="dim">Updated</span><span>${s.last_update ? new Date(s.last_update*1000).toLocaleTimeString() : '—'}</span></div>
      </div>
    </div>`;
  } catch(e) {
    document.getElementById('app').innerHTML = '<pre class="dim">Waiting for bot state...</pre>';
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
