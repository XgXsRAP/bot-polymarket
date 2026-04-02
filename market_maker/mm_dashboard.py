"""
Market Maker Dashboard — two modes:

  python mm_dashboard.py           → Rich terminal UI (default)
  python mm_dashboard.py --web     → aiohttp web server on port 8889

Both read from data/paper_mm_state.json written by the paper trader.
Run the dashboard in a second terminal while the bot runs.
"""

import argparse
import asyncio
import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path

STATE_FILE = "data/paper_mm_state.json"
REFRESH_RATE = 1.0  # seconds


# ═══════════════════════════════════════════════════════════════════
#  SHARED: state reader
# ═══════════════════════════════════════════════════════════════════

def load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


def _bar(value: float, width: int = 20, char: str = "█", empty: str = "░") -> str:
    filled = max(0, min(width, round(value / 100 * width)))
    return char * filled + empty * (width - filled)


def _expiry_bar(seconds: float, total: float = 300.0, width: int = 20) -> str:
    remaining_ratio = max(0.0, min(1.0, seconds / total))
    filled = round(remaining_ratio * width)
    return "█" * filled + "░" * (width - filled)


def _since(ts: float) -> str:
    if not ts:
        return "never"
    age = time.time() - ts
    if age < 60:
        return f"{age:.0f}s ago"
    return f"{age/60:.1f}m ago"


# ═══════════════════════════════════════════════════════════════════
#  MODE 1: Rich terminal dashboard
# ═══════════════════════════════════════════════════════════════════

def run_terminal():
    try:
        from rich.console import Console
        from rich.live import Live
        from rich.table import Table
        from rich.panel import Panel
        from rich.columns import Columns
        from rich import box
        from rich.text import Text
    except ImportError:
        print("rich not installed. Run: venv/bin/pip install rich")
        print("Or use --web mode instead.")
        return

    console = Console()

    def _confidence_color(score: float) -> str:
        if score >= 80: return "bright_green"
        if score >= 60: return "yellow"
        if score >= 40: return "orange3"
        return "bright_red"

    def _pnl_color(val: float) -> str:
        return "bright_green" if val >= 0 else "bright_red"

    def build_layout(s: dict) -> Table:
        root = Table.grid(padding=(0, 1))
        root.add_column(ratio=1)

        # ── Header: BTC price ─────────────────────────────────────
        btc = s.get("btc_price", 0)
        ch1 = s.get("btc_change_1m", 0) * 100
        ch5 = s.get("btc_change_5m", 0) * 100
        vol = s.get("btc_volatility_1m", 0)
        btc_line = Text()
        btc_line.append("  BTC  ", style="bold white")
        btc_line.append(f"${btc:,.2f}  ", style="bold bright_yellow")
        ch1_style = "bright_green" if ch1 >= 0 else "bright_red"
        ch5_style = "bright_green" if ch5 >= 0 else "bright_red"
        btc_line.append(f"({ch1:+.3f}% 1m) ", style=ch1_style)
        btc_line.append(f"({ch5:+.3f}% 5m)  ", style=ch5_style)
        btc_line.append(f"Vol:{vol:.5f}", style="dim")

        header = Panel(btc_line, title="[bold]BTC 5-MIN MARKET MAKER[/bold]",
                       border_style="bright_blue", box=box.DOUBLE_EDGE)
        root.add_row(header)

        # ── Active market + expiry bar ─────────────────────────────
        expiry = s.get("seconds_to_expiry", 300)
        mid = s.get("current_yes_bid", 0)
        mkt_id = s.get("market_id") or "Waiting for market..."
        mins = int(expiry // 60)
        secs = int(expiry % 60)
        bar = _expiry_bar(expiry, 300, 30)
        expiry_color = "bright_red" if expiry < 30 else ("yellow" if expiry < 60 else "bright_green")
        mkt_text = Text()
        mkt_text.append(f"  {mkt_id[:60]}\n", style="white")
        mkt_text.append(f"  {bar}  ", style=expiry_color)
        mkt_text.append(f"Time left: {mins}m {secs:02d}s", style=f"bold {expiry_color}")
        root.add_row(Panel(mkt_text, title="ACTIVE MARKET", border_style="blue"))

        # ── Two columns: signals | quotes ─────────────────────────
        cvd = s.get("cvd_signal", 0)
        fund = s.get("funding_signal", 0)
        liq = s.get("liq_signal", 0)
        oi = s.get("oi_signal", 0)

        def sig_style(v): return "bright_green" if v > 0.1 else ("bright_red" if v < -0.1 else "dim white")
        def sig_arrow(v): return "▲" if v > 0.1 else ("▼" if v < -0.1 else "→")

        sig_table = Table(box=None, show_header=False, padding=(0, 1))
        sig_table.add_column(style="dim", width=12)
        sig_table.add_column(width=8)
        sig_table.add_column(width=12)
        sig_table.add_row("CVD",     f"[{sig_style(cvd)}]{cvd:+.3f} {sig_arrow(cvd)}[/]", "buy/sell flow")
        sig_table.add_row("Funding", f"[{sig_style(-fund)}]{fund:+.3f} {sig_arrow(fund)}[/]", "crowd position")
        sig_table.add_row("Liq",     f"[{sig_style(liq)}]{liq:+.3f} {sig_arrow(liq)}[/]", "liquidations")
        sig_table.add_row("OI",      f"[{sig_style(oi)}]{oi:+.3f} {sig_arrow(oi)}[/]", "new money")
        sig_panel = Panel(sig_table, title="SIGNALS", border_style="cyan")

        yes_bid = s.get("current_yes_bid", 0)
        yes_ask = s.get("current_yes_ask", 0)
        fv = s.get("current_fair_value", 0.5)
        spread = s.get("current_spread", 0)
        q_table = Table(box=None, show_header=False, padding=(0, 1))
        q_table.add_column(style="dim", width=12)
        q_table.add_column()
        q_table.add_row("YES BID",    f"[bright_green]{yes_bid:.4f}[/]  ← posting")
        q_table.add_row("YES ASK",    f"[bright_red]{yes_ask:.4f}[/]  ← posting")
        q_table.add_row("Fair Value", f"[white]{fv:.4f}[/]")
        q_table.add_row("Spread",     f"[yellow]{spread:.4f}[/]")
        q_panel = Panel(q_table, title="QUOTES", border_style="cyan")

        root.add_row(Columns([sig_panel, q_panel]))

        # ── Confidence bar ────────────────────────────────────────
        conf = s.get("current_confidence", 0)
        tier = s.get("confidence_tier", "PAUSED")
        reason = s.get("confidence_reason", "no data")
        conf_color = _confidence_color(conf)
        conf_text = Text()
        conf_text.append(f"  [{_bar(conf, 24)}] ", style=conf_color)
        conf_text.append(f"{conf:.0f}%  ", style=f"bold {conf_color}")
        conf_text.append(f"[{tier}]  ", style=conf_color)
        conf_text.append(reason, style="dim")
        root.add_row(Panel(conf_text, title="CONFIDENCE", border_style=conf_color))

        # ── P&L strip ─────────────────────────────────────────────
        rpnl = s.get("realized_pnl", 0)
        upnl = s.get("unrealized_pnl", 0)
        tpnl = rpnl + upnl
        fills = s.get("total_fills", 0)
        trips = s.get("round_trips", 0)
        wins = s.get("winning_trips", 0)
        wr = (wins / trips * 100) if trips > 0 else 0
        inv = s.get("net_inventory", 0)
        dd = s.get("max_drawdown", 0)
        updated = _since(s.get("last_update", 0))

        pnl_table = Table(box=None, show_header=False, padding=(0, 2))
        for _ in range(6): pnl_table.add_column()
        pnl_table.add_row(
            f"[dim]P&L[/]  [{_pnl_color(tpnl)}]{tpnl:+.2f}[/]",
            f"[dim]Realized[/]  [{_pnl_color(rpnl)}]{rpnl:+.2f}[/]",
            f"[dim]Fills[/]  [white]{fills}[/]",
            f"[dim]Trips[/]  [white]{trips}[/]  [dim]Win[/] [white]{wr:.0f}%[/]",
            f"[dim]Inv[/]  [{'bright_red' if abs(inv) > 200 else 'white'}]{inv:+.0f}[/]",
            f"[dim]MaxDD[/]  [bright_red]{dd:.2f}[/]  [dim]upd {updated}[/]",
        )
        root.add_row(Panel(pnl_table, title="P&L", border_style="green"))

        return root

    with Live(console=console, refresh_per_second=1, screen=True) as live:
        while True:
            s = load_state()
            if not s:
                live.update(Panel(
                    "[dim]Waiting for bot state...\n\nRun:  venv/bin/python3 mm_enhanced1.py --paper[/dim]",
                    title="BTC 5-MIN MARKET MAKER",
                    border_style="dim",
                ))
            else:
                live.update(build_layout(s))
            time.sleep(REFRESH_RATE)


# ═══════════════════════════════════════════════════════════════════
#  MODE 2: aiohttp web dashboard (fallback — no extra deps)
# ═══════════════════════════════════════════════════════════════════

WEB_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>BTC Market Maker</title>
<meta http-equiv="refresh" content="2">
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #0d1117; color: #e6edf3; font-family: 'Courier New', monospace; font-size: 14px; padding: 16px; }
  h1 { color: #58a6ff; border-bottom: 1px solid #30363d; padding-bottom: 8px; margin-bottom: 16px; font-size: 18px; }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 12px; }
  .card { background: #161b22; border: 1px solid #30363d; border-radius: 6px; padding: 12px; }
  .card h2 { color: #8b949e; font-size: 11px; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 10px; }
  .big { font-size: 28px; font-weight: bold; color: #f0f6fc; }
  .green { color: #3fb950; } .red { color: #f85149; } .yellow { color: #d29922; }
  .dim { color: #8b949e; font-size: 12px; }
  .bar-track { background: #21262d; border-radius: 3px; height: 10px; margin: 6px 0; }
  .bar-fill { height: 10px; border-radius: 3px; transition: width 0.5s; }
  .sig-row { display: flex; justify-content: space-between; padding: 3px 0; border-bottom: 1px solid #21262d; }
  .full { grid-column: 1 / -1; }
  .expiry-bar { background: #21262d; border-radius: 3px; height: 16px; position: relative; margin: 8px 0; }
  .expiry-fill { height: 16px; border-radius: 3px; background: #1f6feb; transition: width 1s linear; }
  .expiry-text { position: absolute; top: 0; left: 50%; transform: translateX(-50%); line-height: 16px; font-size: 12px; color: #fff; }
  .conf-score { font-size: 32px; font-weight: bold; }
  table { width: 100%; border-collapse: collapse; }
  td { padding: 4px 6px; border-bottom: 1px solid #21262d; }
  .tag { display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 11px; font-weight: bold; margin-left: 6px; }
  .tag-full { background: #1a4731; color: #3fb950; }
  .tag-reduced { background: #3d2f05; color: #d29922; }
  .tag-cautious { background: #3d2005; color: #f0883e; }
  .tag-paused { background: #3d0509; color: #f85149; }
</style>
</head>
<body>
<h1>⚡ BTC 5-MIN MARKET MAKER</h1>

<script>
async function update() {
  try {
    const r = await fetch('/state');
    const s = await r.json();
    const f = (v, d=2) => v !== undefined ? v.toFixed(d) : '—';
    const pct = v => v >= 0 ? `<span class="green">+${f(v*100,3)}%</span>` : `<span class="red">${f(v*100,3)}%</span>`;
    const pnlFmt = v => v >= 0 ? `<span class="green">+$${f(v)}</span>` : `<span class="red">-$${f(Math.abs(v))}</span>`;

    const exp = s.seconds_to_expiry || 300;
    const expPct = Math.max(0, Math.min(100, exp / 300 * 100));
    const expColor = exp < 30 ? '#f85149' : exp < 60 ? '#d29922' : '#1f6feb';
    const mins = Math.floor(exp/60), secs = Math.floor(exp%60);

    const conf = s.current_confidence || 0;
    const tier = (s.confidence_tier || 'PAUSED').toLowerCase();
    const confColor = conf >= 80 ? '#3fb950' : conf >= 60 ? '#d29922' : conf >= 40 ? '#f0883e' : '#f85149';

    const sigArrow = v => v > 0.1 ? '▲' : v < -0.1 ? '▼' : '→';
    const sigColor = v => v > 0.1 ? 'green' : v < -0.1 ? 'red' : 'dim';

    const trips = s.round_trips || 0;
    const wins = s.winning_trips || 0;
    const wr = trips > 0 ? (wins/trips*100).toFixed(0) : '0';

    document.getElementById('app').innerHTML = `
      <div class="card full">
        <h2>BTC PRICE</h2>
        <span class="big">$${(s.btc_price||0).toLocaleString('en', {minimumFractionDigits:2, maximumFractionDigits:2})}</span>
        &nbsp; ${pct(s.btc_change_1m||0)} 1m &nbsp; ${pct(s.btc_change_5m||0)} 5m
        &nbsp; <span class="dim">vol ${f(s.btc_volatility_1m||0,5)}</span>
      </div>

      <div class="card full">
        <h2>ACTIVE MARKET</h2>
        <div class="dim" style="margin-bottom:6px">${s.market_id || 'Waiting for market...'}</div>
        <div class="expiry-bar">
          <div class="expiry-fill" style="width:${expPct}%;background:${expColor}"></div>
          <div class="expiry-text">${mins}m ${secs.toString().padStart(2,'0')}s remaining</div>
        </div>
      </div>

      <div class="card">
        <h2>SIGNALS</h2>
        <div class="sig-row"><span>CVD</span><span class="${sigColor(s.cvd_signal||0)}">${f(s.cvd_signal||0,3)} ${sigArrow(s.cvd_signal||0)}</span></div>
        <div class="sig-row"><span>Funding</span><span class="${sigColor(s.funding_signal||0)}">${f(s.funding_signal||0,3)} ${sigArrow(s.funding_signal||0)}</span></div>
        <div class="sig-row"><span>Liq</span><span class="${sigColor(s.liq_signal||0)}">${f(s.liq_signal||0,3)} ${sigArrow(s.liq_signal||0)}</span></div>
        <div class="sig-row" style="border:none"><span>OI</span><span class="${sigColor(s.oi_signal||0)}">${f(s.oi_signal||0,3)} ${sigArrow(s.oi_signal||0)}</span></div>
      </div>

      <div class="card">
        <h2>QUOTES</h2>
        <div class="sig-row"><span>YES BID</span><span class="green">${f(s.current_yes_bid||0,4)}</span></div>
        <div class="sig-row"><span>YES ASK</span><span class="red">${f(s.current_yes_ask||0,4)}</span></div>
        <div class="sig-row"><span>Fair Value</span><span>${f(s.current_fair_value||0,4)}</span></div>
        <div class="sig-row" style="border:none"><span>Spread</span><span class="yellow">${f(s.current_spread||0,4)}</span></div>
      </div>

      <div class="card full">
        <h2>CONFIDENCE</h2>
        <div style="display:flex;align-items:center;gap:16px">
          <span class="conf-score" style="color:${confColor}">${conf.toFixed(0)}%</span>
          <span class="tag tag-${tier}">${(s.confidence_tier||'PAUSED')}</span>
          <span class="dim">${s.confidence_reason || ''}</span>
        </div>
        <div class="bar-track"><div class="bar-fill" style="width:${conf}%;background:${confColor}"></div></div>
      </div>

      <div class="card full">
        <h2>P&L</h2>
        <table>
          <tr><td class="dim">Total P&L</td><td>${pnlFmt((s.realized_pnl||0)+(s.unrealized_pnl||0))}</td>
              <td class="dim">Realized</td><td>${pnlFmt(s.realized_pnl||0)}</td>
              <td class="dim">Unrealized</td><td>${pnlFmt(s.unrealized_pnl||0)}</td></tr>
          <tr><td class="dim">Fills</td><td>${s.total_fills||0}</td>
              <td class="dim">Round Trips</td><td>${trips}</td>
              <td class="dim">Win Rate</td><td>${wr}%</td></tr>
          <tr><td class="dim">Inventory</td><td class="${Math.abs(s.net_inventory||0)>200?'red':''}">${f(s.net_inventory||0,0)}</td>
              <td class="dim">Max Drawdown</td><td class="red">$${f(s.max_drawdown||0)}</td>
              <td class="dim">Updated</td><td class="dim">${s.last_update ? new Date(s.last_update*1000).toLocaleTimeString() : '—'}</td></tr>
        </table>
      </div>
    `;
  } catch(e) {
    document.getElementById('app').innerHTML = '<div class="card"><span class="dim">Waiting for bot...</span></div>';
  }
}
update();
setInterval(update, 1000);
</script>
<div id="app"><div class="card"><span class="dim">Loading...</span></div></div>
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
    print(f"Web dashboard running at http://localhost:{port}")
    print("Press Ctrl+C to stop.")
    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await runner.cleanup()


# ═══════════════════════════════════════════════════════════════════
#  Entry point
# ═══════════════════════════════════════════════════════════════════

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
