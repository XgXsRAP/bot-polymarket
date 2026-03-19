"""
╔══════════════════════════════════════════╗
║   POLYMARKET BTC BOT — RICH CLI DASHBOARD║
║   Run: python dashboard.py               ║
║   Bot must run in a separate terminal:   ║
║   python bot.py --mode paper             ║
╚══════════════════════════════════════════╝

HOW IT WORKS:
  bot.py  →  writes  →  data/bot_state.json   (every 1s)
  dashboard reads bot_state.json and renders live
  dashboard writes  →  data/bot_commands.json  (your keystrokes)
  bot.py reads bot_commands.json and reacts

KEYBOARD CONTROLS:
  [p] Pause    [r] Resume    [s] Stop bot
  [c] Toggle ClaudeAI        [q] Quit dashboard
"""

import json
import os
import select
import sys
import termios
import threading
import time
import tty
from datetime import datetime
from pathlib import Path

from rich import box
from rich.align import Align
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# ── Paths ──
STATE_FILE   = "data/bot_state.json"
COMMAND_FILE = "data/bot_commands.json"
TRADES_FILE  = "data/paper_trades.json"
LOG_FILE     = "logs/bot.log"

Path("data").mkdir(exist_ok=True)
Path("logs").mkdir(exist_ok=True)

console = Console()

# ── Shared state ──
_running    = True
_status_msg = ""


# ─────────────────────────────────────────────
#  DATA HELPERS
# ─────────────────────────────────────────────

def load_state() -> dict:
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return {
        "running": False, "paused": False, "mode": "paper",
        "capital": 1000.0, "start_capital": 1000.0,
        "btc_price": 0.0, "btc_change_1m": 0.0, "btc_change_5m": 0.0,
        "wins": 0, "losses": 0, "open_trades": [], "active_markets": [],
        "markets_tracked": 0, "last_latency_ms": 0, "scan_count": 0,
        "consecutive_losses": 0, "claude_enabled": False,
        "claude_regime": "unknown", "claude_multiplier": 1.0,
        "uptime_seconds": 0, "pnl_history": [], "last_updated": None,
    }


def load_trades() -> list:
    try:
        if os.path.exists(TRADES_FILE):
            with open(TRADES_FILE) as f:
                return json.load(f).get("closed", [])
    except Exception:
        pass
    return []


def load_logs(n: int = 22) -> list:
    try:
        if os.path.exists(LOG_FILE):
            with open(LOG_FILE) as f:
                return f.readlines()[-n:]
    except Exception:
        pass
    return []


def send_command(cmd: dict):
    try:
        with open(COMMAND_FILE, "w") as f:
            json.dump({**cmd, "timestamp": time.time()}, f)
    except Exception:
        pass


# ─────────────────────────────────────────────
#  FORMATTERS
# ─────────────────────────────────────────────

def fmt_price(v: float) -> str:
    return f"${v:,.2f}"


def fmt_pct(v: float, d: int = 3) -> str:
    return f"{'+'if v >= 0 else ''}{v:.{d}f}%"


def fmt_uptime(s: int) -> str:
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def pnl_style(v: float) -> str:
    return "bold green" if v >= 0 else "bold red"


def _bar(pct: float, width: int = 18) -> Text:
    filled = int(max(0, min(pct, 100)) / 100 * width)
    color  = "green" if pct < 40 else ("yellow" if pct < 70 else "red")
    t = Text()
    t.append("[", style="dim")
    t.append("█" * filled, style=color)
    t.append("░" * (width - filled), style="dim")
    t.append("]", style="dim")
    return t


# ─────────────────────────────────────────────
#  PANEL BUILDERS
# ─────────────────────────────────────────────

def build_header(state: dict) -> Panel:
    mode    = state.get("mode", "paper").upper()
    running = state.get("running", False)
    paused  = state.get("paused", False)
    uptime  = fmt_uptime(state.get("uptime_seconds", 0))
    updated = state.get("last_updated", "—")
    claude  = state.get("claude_enabled", False)
    regime  = state.get("claude_regime", "unknown").upper()
    mult    = state.get("claude_multiplier", 1.0)

    if not running:
        status, s_style = "● STOPPED", "bold red"
    elif paused:
        status, s_style = "⏸ PAUSED",  "bold yellow"
    else:
        status, s_style = "▶ RUNNING", "bold green"

    t = Text(justify="left")
    t.append("  POLYBOT BTC  ", style="bold white on navy_blue")
    t.append("  ")
    t.append(f" {mode} ", style="bold white on blue")
    t.append("  ")
    t.append(status, style=s_style)
    t.append(f"    uptime {uptime}", style="dim white")
    t.append(f"    last update: {updated}", style="dim")
    if claude:
        t.append(f"    🤖 AI:{regime} {mult:.1f}x", style="bold magenta")
    if _status_msg:
        t.append(f"    [{_status_msg}]", style="bold cyan")

    return Panel(t, box=box.HEAVY_HEAD, style="grey7", padding=(0, 0))


def build_metrics(state: dict) -> Panel:
    capital = state["capital"]
    start   = state["start_capital"]
    pnl     = capital - start
    pnl_pct = pnl / start * 100 if start else 0
    total   = state["wins"] + state["losses"]
    wr      = state["wins"] / total * 100 if total else 0
    btc     = state["btc_price"]
    chg1m   = state["btc_change_1m"] * 100
    chg5m   = state["btc_change_5m"] * 100
    lat     = state.get("last_latency_ms", 0)
    mkts    = state.get("markets_tracked", 0)
    scans   = state.get("scan_count", 0)
    open_n  = len(state.get("open_trades", []))
    consec  = state.get("consecutive_losses", 0)

    t = Table(box=None, show_header=False, padding=(0, 3), expand=True)
    for _ in range(7):
        t.add_column(justify="left")

    t.add_row(
        Text("CAPITAL",    style="dim"),
        Text("BTC PRICE",  style="dim"),
        Text("P&L",        style="dim"),
        Text("WIN RATE",   style="dim"),
        Text("LATENCY",    style="dim"),
        Text("MARKETS",    style="dim"),
        Text("STREAK",     style="dim"),
    )
    t.add_row(
        Text(fmt_price(capital),  style="bold white"),
        Text(fmt_price(btc) if btc > 0 else "—",       style="bold cyan"),
        Text(f"{'+'if pnl>=0 else ''}{pnl:.2f} ({fmt_pct(pnl_pct, 2)})", style=pnl_style(pnl)),
        Text(f"{wr:.1f}%  ({total} trades)" if total else "—  (0 trades)", style="bold white"),
        Text(f"{lat:.0f}ms", style="bold red" if lat > 100 else "bold green"),
        Text(f"{mkts} active / {scans} scans", style="white"),
        Text(f"{consec} losses" if consec else "—", style="bold red" if consec >= 3 else "dim"),
    )
    t.add_row(
        Text(""),
        Text(f"1m {fmt_pct(chg1m)}  5m {fmt_pct(chg5m)}", style=f"dim {'green' if chg1m >= 0 else 'red'}"),
        Text(""),
        Text(f"W:{state['wins']}  L:{state['losses']}", style="dim"),
        Text("target <100ms", style="dim"),
        Text(""),
        Text(""),
    )
    return Panel(t, box=box.SIMPLE_HEAD, padding=(0, 1))


def build_markets(state: dict) -> Panel:
    markets = state.get("active_markets", [])
    btc     = state.get("btc_price", 0.0)

    if not markets:
        body = Text("  Waiting for Polymarket data...", style="dim")
        return Panel(body, title=f"[bold]LIVE MARKETS[/bold]  BTC {fmt_price(btc)}", border_style="blue", box=box.SIMPLE_HEAD)

    t = Table(box=box.SIMPLE, show_header=True, header_style="dim", padding=(0, 1), expand=True)
    t.add_column("SIG",  width=5,  justify="center")
    t.add_column("QUESTION", min_width=28, max_width=42)
    t.add_column("YES",  width=6,  justify="right")
    t.add_column("NO",   width=6,  justify="right")
    t.add_column("EDGE", width=8,  justify="right")
    t.add_column("TIME", width=6,  justify="right")
    t.add_column("●",    width=3,  justify="center")

    for m in markets:
        sig   = m.get("signal", "HOLD")
        yes_p = m.get("yes_price", 0.5)
        no_p  = m.get("no_price", 0.5)
        edge  = m.get("edge_pct", 0.0)
        secs  = m.get("seconds_to_expiry", 0)
        q     = m.get("question", "")[:42]
        in_p  = m.get("in_position", False)

        sig_style  = "bold green" if sig == "YES" else ("bold red" if sig == "NO" else "dim")
        time_style = "bold red"    if secs < 30   else ("yellow"   if secs < 60  else "green")
        mins, srem = secs // 60, secs % 60

        t.add_row(
            Text(sig, style=sig_style),
            Text(q, style="white" if in_p else "dim white"),
            Text(f"{yes_p:.3f}", style="green"),
            Text(f"{no_p:.3f}",  style="red"),
            Text(f"{edge:+.3f}%", style="bold green" if edge > 0 else "dim red"),
            Text(f"{mins}:{srem:02d}", style=time_style),
            Text("●" if in_p else "·", style="magenta" if in_p else "dim"),
        )

    return Panel(t, title=f"[bold]LIVE MARKETS[/bold]  BTC {fmt_price(btc)}", border_style="blue", box=box.SIMPLE_HEAD)


def build_positions(state: dict) -> Panel:
    trades = state.get("open_trades", [])

    if not trades:
        return Panel(
            Text("  No open positions.", style="dim"),
            title="[bold]OPEN POSITIONS[/bold]",
            border_style="blue", box=box.SIMPLE_HEAD,
        )

    t = Table(box=box.SIMPLE, show_header=True, header_style="dim", padding=(0, 1), expand=True)
    t.add_column("SIDE",  width=4)
    t.add_column("QUESTION", min_width=20, max_width=30)
    t.add_column("ENTRY", width=7,  justify="right")
    t.add_column("CUR",   width=7,  justify="right")
    t.add_column("P&L",   width=10, justify="right")
    t.add_column("HELD",  width=5,  justify="right")

    for tr in trades:
        side  = tr.get("side", "?")
        q     = tr.get("question", "")[:30]
        entry = tr.get("entry_price", 0)
        cur   = tr.get("current_price", entry)
        shrs  = tr.get("shares", 0)
        pnl   = (cur - entry) * shrs
        held  = int(time.time() - tr.get("entry_time", time.time()))

        t.add_row(
            Text(side, style="bold green" if side == "YES" else "bold red"),
            Text(q, style="white"),
            Text(f"{entry:.4f}", style="dim"),
            Text(f"{cur:.4f}", style="cyan"),
            Text(f"{'+'if pnl>=0 else ''}{pnl:.4f}", style=pnl_style(pnl)),
            Text(f"{held}s", style="dim"),
        )

    return Panel(t, title="[bold]OPEN POSITIONS[/bold]", border_style="blue", box=box.SIMPLE_HEAD)


def build_risk(state: dict) -> Panel:
    capital  = state["capital"]
    start    = state["start_capital"]
    pnl_pct  = (capital - start) / start * 100 if start else 0
    consec   = state.get("consecutive_losses", 0)
    open_n   = len(state.get("open_trades", []))
    daily_pct = min(abs(min(pnl_pct, 0)) / 2.0 * 100, 100)

    t = Table(box=None, show_header=False, padding=(0, 1), expand=True)
    t.add_column(width=13, style="dim")
    t.add_column()
    t.add_column(width=10, justify="right")

    t.add_row(
        "Daily loss",
        _bar(daily_pct),
        Text(f"{daily_pct:.0f}% / 2%", style="dim"),
    )
    t.add_row(
        "Consec loss",
        _bar(consec / 5 * 100),
        Text(f"{consec}/5", style="bold red" if consec >= 3 else "dim"),
    )
    t.add_row(
        "Positions",
        _bar(open_n / 3 * 100),
        Text(f"{open_n}/3", style="dim"),
    )

    alerts = Text()
    if consec >= 5:
        alerts.append("\n  ⚡ CIRCUIT BREAKER ACTIVE", style="bold red")
    if pnl_pct <= -2.0:
        alerts.append("\n  🛑 DAILY LOSS LIMIT HIT", style="bold red")

    body = Text()
    # We render the table into a group with the alerts below
    from rich.console import Group
    return Panel(
        Group(t, alerts) if (consec >= 5 or pnl_pct <= -2.0) else t,
        title="[bold]RISK METERS[/bold]",
        border_style="yellow", box=box.SIMPLE_HEAD,
    )


def build_stats(trades: list, state: dict) -> Panel:
    if not trades:
        return Panel(
            Text("  Stats appear after first closed trade.", style="dim"),
            title="[bold]SESSION STATS[/bold]",
            border_style="yellow", box=box.SIMPLE_HEAD,
        )

    pnls   = [tr.get("pnl", 0) for tr in trades]
    wins   = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    total  = len(pnls)
    wr     = len(wins) / total * 100 if total else 0
    pf     = abs(sum(wins) / sum(losses)) if losses and sum(losses) != 0 else float("inf")

    # Go-live readiness checks
    checks = [
        ("Win rate > 52%",      wr >= 52,        f"{wr:.1f}%"),
        ("Total trades > 50",   total >= 50,      str(total)),
        ("No circuit breaker",  state.get("consecutive_losses", 0) < 5, "✓" if state.get("consecutive_losses", 0) < 5 else "✗"),
        ("Profit factor > 1.5", pf >= 1.5,        f"{pf:.2f}" if pf != float("inf") else "∞"),
    ]

    t = Table(box=None, show_header=False, padding=(0, 1), expand=True)
    t.add_column(width=16, style="dim")
    t.add_column(justify="right")

    t.add_row("Total P&L",     Text(f"${sum(pnls):+.4f}", style=pnl_style(sum(pnls))))
    t.add_row("Avg win",       Text(f"${sum(wins)/len(wins):+.4f}" if wins else "—",     style="green"))
    t.add_row("Avg loss",      Text(f"${sum(losses)/len(losses):+.4f}" if losses else "—", style="red"))
    t.add_row("Best / Worst",  Text(f"${max(pnls):+.4f}  /  ${min(pnls):+.4f}" if pnls else "—", style="dim"))
    t.add_row("Profit factor", Text(f"{pf:.2f}" if pf != float("inf") else "∞", style="bold green" if pf >= 1.5 else "yellow"))
    t.add_row("", Text(""))
    for label, passed, val in checks:
        icon = "[green]✓[/green]" if passed else "[dim]○[/dim]"
        t.add_row(f"  {label}", Text(f"[{icon}] {val}"))

    return Panel(t, title=f"[bold]SESSION STATS[/bold]  {total} trades  {wr:.1f}% WR", border_style="yellow", box=box.SIMPLE_HEAD)


def build_log() -> Panel:
    lines = load_logs(22)
    t = Text()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if "ERROR" in line:
            t.append(line + "\n", style="red")
        elif "SUCCESS" in line or "connected" in line.lower() or "✅" in line:
            t.append(line + "\n", style="green")
        elif "ENTER" in line:
            t.append(line + "\n", style="bold green")
        elif "EXIT" in line:
            t.append(line + "\n", style="bold cyan")
        elif "WARNING" in line or "RISK" in line or "circuit" in line.lower():
            t.append(line + "\n", style="yellow")
        elif "ClaudeBot" in line or "claude" in line.lower():
            t.append(line + "\n", style="magenta")
        elif "INFO" in line:
            t.append(line + "\n", style="dim white")
        else:
            t.append(line + "\n", style="dim")

    return Panel(t, title="[bold]SIGNAL LOG[/bold]", border_style="bright_black", box=box.SIMPLE_HEAD)


def build_trades(trades: list) -> Panel:
    if not trades:
        return Panel(
            Text("  No closed trades yet.", style="dim"),
            title="[bold]RECENT TRADES[/bold]",
            border_style="bright_black", box=box.SIMPLE_HEAD,
        )

    t = Table(box=box.SIMPLE, show_header=True, header_style="dim", padding=(0, 1), expand=True)
    t.add_column("ID",    width=4,  justify="right")
    t.add_column("SIDE",  width=4)
    t.add_column("ENTRY", width=8,  justify="right")
    t.add_column("EXIT",  width=8,  justify="right")
    t.add_column("P&L",   width=10, justify="right")
    t.add_column("P&L%",  width=8,  justify="right")
    t.add_column("REASON", width=18)

    for tr in reversed(trades[-18:]):
        pnl     = tr.get("pnl", 0)
        pnl_pct = tr.get("pnl_pct", 0) * 100
        side    = tr.get("side", "?")
        reason  = (tr.get("exit_reason", "") or "").replace("_", " ")[:18]
        tid     = str(tr.get("trade_id", "?"))[-4:]

        t.add_row(
            Text(tid, style="dim"),
            Text(side, style="bold green" if side == "YES" else "bold red"),
            Text(f"{tr.get('entry_price', 0):.4f}", style="dim"),
            Text(f"{tr.get('exit_price', 0):.4f}", style="dim"),
            Text(f"{'+'if pnl >= 0 else ''}{pnl:.4f}", style=pnl_style(pnl)),
            Text(fmt_pct(pnl_pct), style="green" if pnl_pct >= 0 else "red"),
            Text(reason, style="dim"),
        )

    return Panel(
        t,
        title=f"[bold]RECENT TRADES[/bold]  last {min(18, len(trades))} of {len(trades)}",
        border_style="bright_black", box=box.SIMPLE_HEAD,
    )


def build_controls(state: dict) -> Text:
    claude = state.get("claude_enabled", False)
    t = Text(justify="center")
    t.append("[p]", style="bold yellow");  t.append(" Pause  ")
    t.append("[r]", style="bold green");   t.append(" Resume  ")
    t.append("[s]", style="bold red");     t.append(" Stop bot  ")
    t.append("[c]", style="bold magenta"); t.append(f" Claude AI ({'ON' if claude else 'OFF'})  ")
    t.append("[q]", style="bold white");   t.append(" Quit dashboard")
    return t


def build_screen(state: dict, trades: list) -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="header",   size=3),
        Layout(name="metrics",  size=6),
        Layout(name="row_mid",  size=14),
        Layout(name="row_bot",  size=20),
        Layout(name="controls", size=1),
    )

    layout["row_mid"].split_row(
        Layout(name="markets",   ratio=3),
        Layout(name="positions", ratio=2),
    )
    layout["row_bot"].split_row(
        Layout(name="risk_stats", ratio=1),
        Layout(name="log",        ratio=2),
        Layout(name="trades",     ratio=2),
    )
    layout["risk_stats"].split_column(
        Layout(name="risk",  ratio=1),
        Layout(name="stats", ratio=2),
    )

    layout["header"].update(build_header(state))
    layout["metrics"].update(build_metrics(state))
    layout["markets"].update(build_markets(state))
    layout["positions"].update(build_positions(state))
    layout["risk"].update(build_risk(state))
    layout["stats"].update(build_stats(trades, state))
    layout["log"].update(build_log())
    layout["trades"].update(build_trades(trades))
    layout["controls"].update(Align.center(build_controls(state)))

    return layout


# ─────────────────────────────────────────────
#  KEYBOARD INPUT  (raw terminal, background thread)
# ─────────────────────────────────────────────

def _keyboard_loop():
    global _running, _status_msg
    try:
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            while _running:
                rlist, _, _ = select.select([sys.stdin], [], [], 0.1)
                if not rlist:
                    continue
                ch = sys.stdin.read(1).lower()
                if ch == "q":
                    _running = False
                elif ch == "p":
                    send_command({"action": "pause"})
                    _status_msg = "pause sent"
                elif ch == "r":
                    send_command({"action": "start"})
                    _status_msg = "resume sent"
                elif ch == "s":
                    send_command({"action": "stop"})
                    _status_msg = "stop sent"
                elif ch == "c":
                    cur = load_state().get("claude_enabled", False)
                    send_command({"action": "set_claude", "value": not cur})
                    _status_msg = f"Claude AI {'OFF' if cur else 'ON'}"
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
    except Exception:
        # Windows or non-TTY — keyboard input disabled, display-only mode
        pass


# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────

def main():
    global _running

    kb = threading.Thread(target=_keyboard_loop, daemon=True)
    kb.start()

    with Live(console=console, screen=True, refresh_per_second=2) as live:
        while _running:
            state  = load_state()
            trades = load_trades()
            try:
                live.update(build_screen(state, trades))
            except Exception as e:
                live.update(Panel(f"[red]Render error: {e}[/red]"))
            time.sleep(0.5)

    console.print("\n[dim]Dashboard closed.[/dim]\n")


if __name__ == "__main__":
    main()
