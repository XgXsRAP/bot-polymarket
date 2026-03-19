"""
POLYMARKET BTC UP/DOWN — RICH TERMINAL DASHBOARD
Run: python dashboard.py

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
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from rich import box
from rich.align import Align
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.progress import BarColumn, Progress, TaskID
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

# ── Paths ──
STATE_FILE   = "data/bot_state.json"
COMMAND_FILE = "data/bot_commands.json"
TRADES_FILE  = "data/paper_trades.json"

Path("data").mkdir(exist_ok=True)

console = Console()

# ── Shared state ──
_running    = True
_status_msg = ""

ET = ZoneInfo("America/New_York")

# ─────────────────────────────────────────────
#  POLYMARKET + BTC DATA  (fetched in background)
# ─────────────────────────────────────────────

class MarketState:
    """Thread-safe container for all live market data."""
    def __init__(self):
        self.lock = threading.Lock()
        # Market info
        self.question        = "Fetching market..."
        self.condition_id    = ""
        self.end_ts          = 0.0
        # Prices
        self.up_bid          = 0.0
        self.up_ask          = 0.0
        self.down_bid        = 0.0
        self.down_ask        = 0.0
        self.price_to_beat   = 0.0   # BTC price at market open
        self.btc_price       = 0.0
        self.btc_ts          = 0.0
        # Meta
        self.last_poly_fetch = 0.0
        self.last_btc_fetch  = 0.0
        self.error           = ""

    def snapshot(self) -> dict:
        with self.lock:
            return self.__dict__.copy()


_mkt = MarketState()


def _fetch_btc_price() -> float:
    """Fetch current BTC/USDT price from Binance."""
    try:
        r = requests.get(
            "https://api.binance.com/api/v3/ticker/price",
            params={"symbol": "BTCUSDT"},
            timeout=4,
        )
        return float(r.json()["price"])
    except Exception:
        return 0.0


def _fetch_active_market() -> dict | None:
    """
    Find the currently active BTC Up/Down 5-min market on Polymarket.
    Returns the best (soonest-expiring) market dict, or None.
    """
    try:
        r = requests.get(
            "https://gamma-api.polymarket.com/markets",
            params={"active": "true", "limit": 200},
            timeout=6,
        )
        markets = r.json()
    except Exception as e:
        return None

    now = time.time()
    candidates = []
    for m in markets:
        q = m.get("question", "").lower()
        if not (("btc" in q or "bitcoin" in q) and ("up or down" in q or "5" in q)):
            continue
        end_date = m.get("endDate", "")
        try:
            end_ts = datetime.fromisoformat(end_date.replace("Z", "+00:00")).timestamp()
        except Exception:
            continue
        secs_left = end_ts - now
        if 10 < secs_left < 360:          # only markets with <6 min left
            candidates.append((secs_left, m))

    if not candidates:
        # Nothing imminent — grab the next upcoming one (smallest end_ts > now)
        for m in markets:
            q = m.get("question", "").lower()
            if not (("btc" in q or "bitcoin" in q) and ("up or down" in q or "5" in q)):
                continue
            end_date = m.get("endDate", "")
            try:
                end_ts = datetime.fromisoformat(end_date.replace("Z", "+00:00")).timestamp()
            except Exception:
                continue
            if end_ts > now:
                candidates.append((end_ts - now, m))

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]


def _fetch_clob_orderbook(token_id: str) -> tuple[float, float]:
    """
    Fetch best bid/ask from CLOB for a given token.
    Returns (best_bid, best_ask).
    """
    try:
        r = requests.get(
            f"https://clob.polymarket.com/book",
            params={"token_id": token_id},
            timeout=4,
        )
        data = r.json()
        bids = data.get("bids", [])
        asks = data.get("asks", [])
        best_bid = float(bids[0]["price"]) if bids else 0.0
        best_ask = float(asks[0]["price"]) if asks else 0.0
        return best_bid, best_ask
    except Exception:
        return 0.0, 0.0


def _data_loop():
    """Background thread: refreshes market data and BTC price."""
    global _mkt
    while _running:
        now = time.time()

        # ── BTC price — every 2 seconds ──
        if now - _mkt.last_btc_fetch >= 2:
            price = _fetch_btc_price()
            with _mkt.lock:
                if price > 0:
                    _mkt.btc_price    = price
                    _mkt.btc_ts       = now
                    _mkt.last_btc_fetch = now
                    # Set price_to_beat on first fetch for this market window
                    if _mkt.price_to_beat == 0.0:
                        _mkt.price_to_beat = price

        # ── Polymarket market + orderbook — every 5 seconds ──
        if now - _mkt.last_poly_fetch >= 5:
            m = _fetch_active_market()
            if m:
                tokens    = m.get("tokens", [])
                up_tok_id = ""
                dn_tok_id = ""
                up_price  = 0.0
                dn_price  = 0.0

                for tok in tokens:
                    outcome = tok.get("outcome", "").upper()
                    if outcome == "YES":
                        up_tok_id = tok.get("token_id", "")
                        up_price  = float(tok.get("price", 0.5))
                    elif outcome == "NO":
                        dn_tok_id = tok.get("token_id", "")
                        dn_price  = float(tok.get("price", 0.5))

                # Try CLOB orderbook for real bid/ask
                up_bid, up_ask = (up_price, up_price)
                dn_bid, dn_ask = (dn_price, dn_price)
                if up_tok_id:
                    b, a = _fetch_clob_orderbook(up_tok_id)
                    if b > 0 or a > 0:
                        up_bid, up_ask = b, a
                if dn_tok_id:
                    b, a = _fetch_clob_orderbook(dn_tok_id)
                    if b > 0 or a > 0:
                        dn_bid, dn_ask = b, a

                end_date = m.get("endDate", "")
                try:
                    end_ts = datetime.fromisoformat(end_date.replace("Z", "+00:00")).timestamp()
                except Exception:
                    end_ts = now + 300

                # Reset price_to_beat when a new market window begins
                old_cid = _mkt.condition_id
                new_cid = m.get("conditionId", "")

                with _mkt.lock:
                    if new_cid != old_cid:
                        # New market — reset anchor price
                        _mkt.price_to_beat = _mkt.btc_price if _mkt.btc_price > 0 else 0.0
                    _mkt.condition_id    = new_cid
                    _mkt.question        = m.get("question", "")
                    _mkt.end_ts          = end_ts
                    _mkt.up_bid          = up_bid
                    _mkt.up_ask          = up_ask
                    _mkt.down_bid        = dn_bid
                    _mkt.down_ask        = dn_ask
                    _mkt.last_poly_fetch = now
                    _mkt.error           = ""
            else:
                with _mkt.lock:
                    _mkt.error = "No active BTC 5m market found"
                    _mkt.last_poly_fetch = now

        time.sleep(0.25)


# ─────────────────────────────────────────────
#  BOT STATE  (from shared JSON)
# ─────────────────────────────────────────────

def load_bot_state() -> dict:
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return {"running": False, "paused": False, "mode": "paper",
            "capital": 1000.0, "start_capital": 1000.0, "wins": 0, "losses": 0,
            "open_trades": [], "uptime_seconds": 0}


def send_command(cmd: dict):
    try:
        with open(COMMAND_FILE, "w") as f:
            json.dump({**cmd, "timestamp": time.time()}, f)
    except Exception:
        pass


# ─────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────

def fmt_price(v: float) -> str:
    return f"${v:,.2f}"


def _parse_question_time(question: str) -> str:
    """
    Try to extract the time window from question like
    'Bitcoin Up or Down - March 17, 11:20AM-11:25AM ET'
    and return a clean label.
    """
    if not question:
        return "BTC Up or Down"
    # Strip 'Will ' prefix if present, keep as-is otherwise
    q = question.replace("Will ", "").strip()
    return q[:70]


# ─────────────────────────────────────────────
#  SCREEN BUILDER
# ─────────────────────────────────────────────

def build_screen(snap: dict, bot: dict) -> Group:
    now     = time.time()
    secs    = max(0.0, snap["end_ts"] - now)
    total   = 300.0   # 5-min window
    pct     = max(0.0, min(1.0, secs / total))

    btc      = snap["btc_price"]
    anchor   = snap["price_to_beat"]
    diff     = btc - anchor
    diff_pct = diff / anchor * 100 if anchor > 0 else 0.0

    up_bid    = snap["up_bid"]
    up_ask    = snap["up_ask"]
    dn_bid    = snap["down_bid"]
    dn_ask    = snap["down_ask"]
    up_spread = up_ask - up_bid
    dn_spread = dn_ask - dn_bid

    direction = ">>> ABOVE <<<" if diff >= 0 else ">>> BELOW <<<"
    dir_style = "bold green" if diff >= 0 else "bold red"

    question_label = _parse_question_time(snap["question"])
    mins  = int(secs) // 60
    secs2 = int(secs) % 60

    # ── Panel 1: Market header ──────────────────────────────────────
    title_text = Text(justify="center")
    title_text.append("  ")
    title_text.append("₿", style="bold yellow")
    title_text.append(f"  {question_label}", style="bold white")

    header = Panel(
        title_text,
        box=box.DOUBLE_EDGE,
        style="on grey7",
        padding=(0, 1),
    )

    # ── Panel 2: Price comparison ────────────────────────────────────
    price_table = Table(box=None, show_header=False, padding=(0, 2), expand=True)
    price_table.add_column(width=20, style="dim")
    price_table.add_column()

    price_table.add_row(
        Text("▶ PRICE TO BEAT", style="bold white"),
        Text(fmt_price(anchor) if anchor > 0 else "—", style="bold yellow"),
    )
    price_table.add_row(
        Text("B  CURRENT BTC",  style="bold cyan"),
        Text(fmt_price(btc)   if btc    > 0 else "—", style="bold cyan"),
    )

    diff_val = Text()
    diff_val.append(
        f"{'+'if diff>=0 else ''}{fmt_price(diff)}  ({'+' if diff_pct>=0 else ''}{diff_pct:.3f}%)",
        style="bold green" if diff >= 0 else "bold red",
    )
    price_table.add_row(Text("▼  DIFF", style="bold white"), diff_val)
    price_table.add_row(Text(""), Text(direction, style=dir_style))

    price_panel = Panel(price_table, box=box.SQUARE, style="on grey7", padding=(0, 2))

    # ── Panel 3: UP / DOWN odds ──────────────────────────────────────
    odds_table = Table(box=None, show_header=False, padding=(0, 1), expand=True)
    odds_table.add_column(width=6)
    odds_table.add_column()

    def odds_row(label: str, bid: float, ask: float, spread: float, color: str):
        t = Text()
        t.append(f"  Bid: ", style="dim"); t.append(f"${bid:.4f}", style=f"bold {color}")
        t.append(f"  |  Ask: ", style="dim"); t.append(f"${ask:.4f}", style=f"bold {color}")
        t.append(f"  |  Spread: ", style="dim"); t.append(f"${spread:.4f}", style=color)
        return Text(label, style=f"bold {color}"), t

    lbl, row = odds_row("● UP",   up_bid, up_ask, up_spread, "green")
    odds_table.add_row(lbl, row)
    lbl, row = odds_row("● DOWN", dn_bid, dn_ask, dn_spread, "red")
    odds_table.add_row(lbl, row)

    odds_panel = Panel(odds_table, box=box.SQUARE, style="on grey7", padding=(0, 2))

    # ── Panel 4: Bot status strip ────────────────────────────────────
    capital     = bot.get("capital", 1000.0)
    start_cap   = bot.get("start_capital", 1000.0)
    pnl         = capital - start_cap
    wins        = bot.get("wins", 0)
    losses      = bot.get("losses", 0)
    mode        = bot.get("mode", "paper").upper()
    running     = bot.get("running", False)
    paused      = bot.get("paused", False)
    open_n      = len(bot.get("open_trades", []))
    uptime      = bot.get("uptime_seconds", 0)
    uptime_str  = f"{uptime//3600:02d}:{(uptime%3600)//60:02d}:{uptime%60:02d}"

    if not running:
        run_txt, run_col = "STOPPED", "red"
    elif paused:
        run_txt, run_col = "PAUSED",  "yellow"
    else:
        run_txt, run_col = "RUNNING", "green"

    bot_line = Text()
    bot_line.append(f" {mode} ", style="bold white on blue")
    bot_line.append("  ")
    bot_line.append(run_txt, style=f"bold {run_col}")
    bot_line.append(f"  {uptime_str}  │  ", style="dim")
    bot_line.append(f"Capital: {fmt_price(capital)}", style="bold white")
    bot_line.append(f"  P&L: {'+'if pnl>=0 else ''}{pnl:.2f}", style="bold green" if pnl >= 0 else "bold red")
    bot_line.append(f"  │  W:{wins}  L:{losses}  │  Open: {open_n}/3", style="dim")
    if snap["error"]:
        bot_line.append(f"  ⚠ {snap['error']}", style="yellow")

    bot_panel = Panel(bot_line, box=box.SQUARE, style="on grey7", padding=(0, 1))

    # ── Panel 5: CTA ────────────────────────────────────────────────
    cta_text = Text(justify="center")
    cta_text.append("READY TO BET!\n", style="bold yellow")
    cta_text.append("  Press ", style="dim")
    cta_text.append("[B]", style="bold green")
    cta_text.append(" to buy UP   |   Press ", style="dim")
    cta_text.append("[S]", style="bold red")
    cta_text.append(" to buy DOWN", style="dim")

    cta_panel = Panel(cta_text, box=box.SQUARE, style="on grey7", padding=(0, 2))

    # ── Panel 6: Countdown ──────────────────────────────────────────
    # Progress bar
    bar_width   = 46
    filled      = int(pct * bar_width)
    empty       = bar_width - filled
    bar_color   = "red" if secs < 30 else ("yellow" if secs < 60 else "green")

    bar_text = Text()
    bar_text.append("  TIME LEFT: ", style="bold white")
    bar_text.append(f"{mins}:{secs2:02d}   ", style=f"bold {bar_color}")
    bar_text.append("[", style="dim")
    bar_text.append("█" * filled, style=bar_color)
    bar_text.append("░" * empty,  style="dim")
    bar_text.append("]", style="dim")

    # Big clock on right
    clock_text = Text(justify="right")
    clock_text.append(f"{mins}:{secs2:02d}", style=f"bold {bar_color}")

    timer_table = Table(box=None, show_header=False, padding=(0, 0), expand=True)
    timer_table.add_column(ratio=3)
    timer_table.add_column(ratio=1, justify="right")
    timer_table.add_row(bar_text, Text(f"\n{mins}:{secs2:02d}", style=f"bold {bar_color} on grey7", justify="right"))

    countdown_panel = Panel(timer_table, box=box.SQUARE, style="on grey7", padding=(0, 1))

    # ── Panel 7: Controls ───────────────────────────────────────────
    ctrl = Text(justify="center")
    ctrl.append("[B]", style="bold green");   ctrl.append(" UP  ")
    ctrl.append("[S]", style="bold red");     ctrl.append(" DOWN  ")
    ctrl.append("[P]", style="bold yellow");  ctrl.append(" Pause  ")
    ctrl.append("[R]", style="bold cyan");    ctrl.append(" Resume  ")
    ctrl.append("[Q]", style="bold white");   ctrl.append(" Quit")
    if _status_msg:
        ctrl.append(f"   [{_status_msg}]", style="bold cyan")

    return Group(
        header,
        price_panel,
        odds_panel,
        bot_panel,
        cta_panel,
        countdown_panel,
        Align.center(ctrl),
    )


# ─────────────────────────────────────────────
#  KEYBOARD  (raw terminal, background thread)
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
                    _status_msg = "pause sent"
                elif ch == "r":
                    send_command({"action": "start"})
                    _status_msg = "resume sent"
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
    except Exception:
        pass   # non-TTY / Windows — display-only mode


# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────

def main():
    global _running

    # Start background data thread
    data_thread = threading.Thread(target=_data_loop, daemon=True)
    data_thread.start()

    # Start keyboard thread
    kb_thread = threading.Thread(target=_keyboard_loop, daemon=True)
    kb_thread.start()

    with Live(console=console, screen=True, refresh_per_second=4) as live:
        while _running:
            snap = _mkt.snapshot()
            bot  = load_bot_state()
            try:
                live.update(build_screen(snap, bot))
            except Exception as e:
                live.update(Panel(f"[red]Render error: {e}[/red]"))
            time.sleep(0.25)

    console.print("\n[dim]Dashboard closed.[/dim]\n")


if __name__ == "__main__":
    main()
