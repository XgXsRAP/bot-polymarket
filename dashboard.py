"""
POLYMARKET BTC UP/DOWN — COMPACT TERMINAL DASHBOARD
Run: python3 dashboard.py

Keyboard controls:
  [B] Buy UP      [S] Buy DOWN
  [P] Pause bot   [R] Resume bot
  [Q] Quit
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
from zoneinfo import ZoneInfo

import requests
from rich import box
from rich.align import Align
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# ── Paths ──
STATE_FILE   = "data/bot_state.json"
COMMAND_FILE = "data/bot_commands.json"
Path("data").mkdir(exist_ok=True)

console = Console()

_running    = True
_status_msg = ""

# ─────────────────────────────────────────────
#  MARKET STATE
# ─────────────────────────────────────────────

class MarketState:
    def __init__(self):
        self.lock            = threading.Lock()
        self.question        = ""
        self.condition_id    = ""
        self.end_ts          = 0.0
        self.up_bid          = 0.0
        self.up_ask          = 0.0
        self.down_bid        = 0.0
        self.down_ask        = 0.0
        self.price_to_beat   = 0.0
        self.btc_price       = 0.0
        self.last_poly_fetch = 0.0
        self.last_btc_fetch  = 0.0
        self.error           = ""

    def snapshot(self):
        with self.lock:
            return self.__dict__.copy()

_mkt = MarketState()


def _fetch_btc_price():
    try:
        r = requests.get(
            "https://api.binance.com/api/v3/ticker/price",
            params={"symbol": "BTCUSDT"}, timeout=4,
        )
        return float(r.json()["price"])
    except Exception:
        return 0.0


def _fetch_active_market():
    try:
        r = requests.get(
            "https://gamma-api.polymarket.com/markets",
            params={"active": "true", "limit": 200}, timeout=6,
        )
        markets = r.json()
    except Exception:
        return None

    now = time.time()
    candidates = []
    for m in markets:
        q = m.get("question", "").lower()
        if not (("btc" in q or "bitcoin" in q) and ("up or down" in q)):
            continue
        try:
            end_ts = datetime.fromisoformat(
                m.get("endDate", "").replace("Z", "+00:00")
            ).timestamp()
        except Exception:
            continue
        secs_left = end_ts - now
        if secs_left > 10:
            candidates.append((secs_left, m))

    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]


def _fetch_clob_orderbook(token_id):
    try:
        r = requests.get(
            "https://clob.polymarket.com/book",
            params={"token_id": token_id}, timeout=4,
        )
        data = r.json()
        bids = data.get("bids", [])
        asks = data.get("asks", [])
        return (float(bids[0]["price"]) if bids else 0.0,
                float(asks[0]["price"]) if asks else 0.0)
    except Exception:
        return 0.0, 0.0


def _data_loop():
    while _running:
        now = time.time()

        if now - _mkt.last_btc_fetch >= 2:
            price = _fetch_btc_price()
            with _mkt.lock:
                if price > 0:
                    _mkt.btc_price      = price
                    _mkt.last_btc_fetch = now
                    if _mkt.price_to_beat == 0.0:
                        _mkt.price_to_beat = price

        if now - _mkt.last_poly_fetch >= 5:
            m = _fetch_active_market()
            if m:
                tokens = m.get("tokens", [])
                up_tok, dn_tok = "", ""
                up_p, dn_p     = 0.0, 0.0
                for tok in tokens:
                    out = tok.get("outcome", "").upper()
                    if out == "YES":
                        up_tok = tok.get("token_id", "")
                        up_p   = float(tok.get("price", 0.5))
                    elif out == "NO":
                        dn_tok = tok.get("token_id", "")
                        dn_p   = float(tok.get("price", 0.5))

                ub, ua = (up_p, up_p)
                db, da = (dn_p, dn_p)
                if up_tok:
                    b, a = _fetch_clob_orderbook(up_tok)
                    if b > 0 or a > 0: ub, ua = b, a
                if dn_tok:
                    b, a = _fetch_clob_orderbook(dn_tok)
                    if b > 0 or a > 0: db, da = b, a

                try:
                    end_ts = datetime.fromisoformat(
                        m.get("endDate", "").replace("Z", "+00:00")
                    ).timestamp()
                except Exception:
                    end_ts = now + 300

                new_cid = m.get("conditionId", "")
                with _mkt.lock:
                    if new_cid != _mkt.condition_id:
                        _mkt.price_to_beat = _mkt.btc_price or 0.0
                    _mkt.condition_id    = new_cid
                    _mkt.question        = m.get("question", "")
                    _mkt.end_ts          = end_ts
                    _mkt.up_bid          = ub
                    _mkt.up_ask          = ua
                    _mkt.down_bid        = db
                    _mkt.down_ask        = da
                    _mkt.last_poly_fetch = now
                    _mkt.error           = ""
            else:
                with _mkt.lock:
                    _mkt.error           = "No active BTC market found"
                    _mkt.last_poly_fetch = now

        time.sleep(0.25)


# ─────────────────────────────────────────────
#  BOT STATE
# ─────────────────────────────────────────────

def load_bot_state():
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return {"running": False, "paused": False, "mode": "paper",
            "capital": 1000.0, "start_capital": 1000.0,
            "wins": 0, "losses": 0, "open_trades": [], "uptime_seconds": 0}


def send_command(cmd: dict):
    try:
        with open(COMMAND_FILE, "w") as f:
            json.dump({**cmd, "timestamp": time.time()}, f)
    except Exception:
        pass


# ─────────────────────────────────────────────
#  SCREEN BUILDER  — single compact panel
# ─────────────────────────────────────────────

def _p(v: float) -> str:
    return f"${v:,.2f}"


def build_screen(snap: dict, bot: dict) -> Panel:
    now   = time.time()
    secs  = max(0.0, snap["end_ts"] - now)
    pct   = max(0.0, min(1.0, secs / 300.0))
    mins  = int(secs) // 60
    secs2 = int(secs) % 60

    btc    = snap["btc_price"]
    anchor = snap["price_to_beat"]
    diff   = btc - anchor
    dpct   = diff / anchor * 100 if anchor > 0 else 0.0

    up_bid, up_ask = snap["up_bid"],   snap["up_ask"]
    dn_bid, dn_ask = snap["down_bid"], snap["down_ask"]

    # colours
    diff_col  = "green" if diff >= 0 else "red"
    timer_col = "red"   if secs < 30 else ("yellow" if secs < 60 else "green")

    # bot fields
    capital   = bot.get("capital", 1000.0)
    start_cap = bot.get("start_capital", 1000.0)
    pnl       = capital - start_cap
    wins      = bot.get("wins", 0)
    losses    = bot.get("losses", 0)
    mode      = bot.get("mode", "paper").upper()
    running   = bot.get("running", False)
    paused    = bot.get("paused", False)
    open_n    = len(bot.get("open_trades", []))
    uptime    = bot.get("uptime_seconds", 0)
    uptime_s  = f"{uptime//3600:02d}:{(uptime%3600)//60:02d}:{uptime%60:02d}"

    if not running:   run_txt, run_col = "STOPPED", "red"
    elif paused:      run_txt, run_col = "PAUSED",  "yellow"
    else:             run_txt, run_col = "RUNNING", "green"

    # shorten question
    q = snap["question"].replace("Will ", "").strip()
    q = q[:65] if q else "Waiting for market..."

    # ── build one table with all rows ──────────────────────────────
    t = Table(box=None, show_header=False, padding=(0, 1), expand=True)
    t.add_column(width=16)
    t.add_column()

    # row helper
    def row(label: str, val: Text | str, label_style="dim"):
        lbl = Text(label, style=label_style)
        v   = val if isinstance(val, Text) else Text(val)
        t.add_row(lbl, v)

    def sep():
        t.add_row(Text("", style="grey30"), Text("─" * 55, style="grey30"))

    # ── MARKET TITLE ──
    title = Text(justify="center")
    title.append("₿ ", style="bold yellow")
    title.append(q, style="bold white")
    t.add_row(Text(""), title)

    sep()

    # ── BTC PRICES ──
    row("▶ PRICE TO BEAT",
        Text(_p(anchor) if anchor > 0 else "—", style="bold yellow"),
        "bold white")
    row("  CURRENT BTC",
        Text(_p(btc) if btc > 0 else "fetching...", style="bold cyan"),
        "bold cyan")

    diff_txt = Text()
    if anchor > 0 and btc > 0:
        diff_txt.append(f"{'+'if diff>=0 else ''}{_p(diff)}", style=f"bold {diff_col}")
        diff_txt.append(f"  ({'+' if dpct>=0 else ''}{dpct:.3f}%)", style=diff_col)
        diff_txt.append("  ")
        diff_txt.append("ABOVE" if diff >= 0 else "BELOW", style=f"bold {diff_col} reverse")
    else:
        diff_txt.append("—", style="dim")
    row("  DIFF", diff_txt, "bold white")

    sep()

    # ── ODDS ──
    up_line = Text()
    up_line.append(f"Bid {up_bid:.4f}", style="bold green")
    up_line.append("  |  ", style="dim")
    up_line.append(f"Ask {up_ask:.4f}", style="bold green")
    up_line.append("  |  ", style="dim")
    up_line.append(f"Sprd {up_ask-up_bid:.4f}", style="green")
    row("● UP  ", up_line, "bold green")

    dn_line = Text()
    dn_line.append(f"Bid {dn_bid:.4f}", style="bold red")
    dn_line.append("  |  ", style="dim")
    dn_line.append(f"Ask {dn_ask:.4f}", style="bold red")
    dn_line.append("  |  ", style="dim")
    dn_line.append(f"Sprd {dn_ask-dn_bid:.4f}", style="red")
    row("● DOWN", dn_line, "bold red")

    sep()

    # ── BOT STATUS ──
    bot_left = Text()
    bot_left.append(f" {mode} ", style="bold white on blue")
    bot_left.append(f" {run_txt}", style=f"bold {run_col}")
    bot_left.append(f" {uptime_s}", style="dim")

    bot_right = Text()
    bot_right.append(_p(capital), style="bold white")
    bot_right.append(f"  P&L: {'+'if pnl>=0 else ''}{pnl:.2f}", style=f"bold {'green' if pnl>=0 else 'red'}")
    bot_right.append(f"  W:{wins} L:{losses}", style="dim")
    bot_right.append(f"  Open:{open_n}/3", style="dim")
    t.add_row(bot_left, bot_right)

    if snap["error"]:
        row("", Text(f"⚠  {snap['error']}", style="yellow"))

    sep()

    # ── COUNTDOWN BAR ──
    bar_w  = 36
    filled = int(pct * bar_w)
    empty  = bar_w - filled
    bar = Text()
    bar.append("█" * filled, style=timer_col)
    bar.append("░" * empty,  style="grey30")

    timer_right = Text()
    timer_right.append(f"{mins}:{secs2:02d}", style=f"bold {timer_col}")
    t.add_row(Text("  TIME LEFT", style="bold white"), Text(f"{mins}:{secs2:02d}  ", style=f"bold {timer_col}") + bar)

    sep()

    # ── CTA + CONTROLS ──
    cta = Text(justify="center")
    cta.append("READY TO BET!  ", style="bold yellow")
    cta.append("[B]", style="bold green");  cta.append(" UP   ", style="dim")
    cta.append("[S]", style="bold red");    cta.append(" DOWN   ", style="dim")
    cta.append("[P]", style="bold yellow"); cta.append(" Pause   ", style="dim")
    cta.append("[R]", style="bold cyan");   cta.append(" Resume   ", style="dim")
    cta.append("[Q]", style="bold white");  cta.append(" Quit", style="dim")
    if _status_msg:
        cta.append(f"   ✓ {_status_msg}", style="bold cyan")
    t.add_row(Text(""), cta)

    return Panel(t, box=box.DOUBLE_EDGE, padding=(0, 1),
                 title="[bold yellow]POLYMARKET[/bold yellow]",
                 subtitle="[dim]live[/dim]")


# ─────────────────────────────────────────────
#  KEYBOARD
# ─────────────────────────────────────────────

def _keyboard_loop():
    global _running, _status_msg
    try:
        fd  = sys.stdin.fileno()
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
                elif ch == "b":
                    send_command({"action": "buy", "side": "YES"})
                    _status_msg = "BUY UP sent"
                elif ch == "s":
                    send_command({"action": "buy", "side": "NO"})
                    _status_msg = "BUY DOWN sent"
                elif ch == "p":
                    send_command({"action": "pause"})
                    _status_msg = "paused"
                elif ch == "r":
                    send_command({"action": "start"})
                    _status_msg = "resumed"
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
    except Exception:
        pass


# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────

def main():
    global _running

    threading.Thread(target=_data_loop,     daemon=True).start()
    threading.Thread(target=_keyboard_loop, daemon=True).start()

    with Live(console=console, screen=True, refresh_per_second=4) as live:
        while _running:
            try:
                live.update(build_screen(_mkt.snapshot(), load_bot_state()))
            except Exception as e:
                live.update(Panel(f"[red]Error: {e}[/red]"))
            time.sleep(0.25)

    console.print("\n[dim]Dashboard closed.[/dim]\n")


if __name__ == "__main__":
    main()
