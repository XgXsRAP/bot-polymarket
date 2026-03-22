#!/usr/bin/env python3
"""
POLYMARKET BTC UP/DOWN — HTML WEB DASHBOARD
Run:  python3 dashboard.py
Open: http://localhost:8888

Keyboard (in browser):
  B → Buy UP    S → Buy DOWN
  P → Pause     R → Resume    Q → (n/a — just close tab)
"""

import json
import os
import time
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

STATE_FILE   = "data/bot_state.json"
COMMAND_FILE = "data/bot_commands.json"
Path("data").mkdir(exist_ok=True)

PORT = 8888

# ─────────────────────────────────────────────────────────────────────────────
#  HTML PAGE  (served once; JS polls /state every second)
# ─────────────────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Polymarket BTC Bot</title>
<style>
  :root {
    --bg:      #0d1117;
    --bg2:     #161b22;
    --bg3:     #21262d;
    --border:  #30363d;
    --text:    #e6edf3;
    --dim:     #7d8590;
    --green:   #3fb950;
    --red:     #f85149;
    --yellow:  #d29922;
    --cyan:    #79c0ff;
    --orange:  #f0883e;
    --purple:  #bc8cff;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: var(--bg);
    color: var(--text);
    font-family: 'Courier New', Courier, monospace;
    font-size: 13px;
    padding: 12px;
  }
  h2 {
    color: var(--yellow);
    font-size: 15px;
    letter-spacing: 1px;
    margin-bottom: 8px;
    text-transform: uppercase;
  }
  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
    gap: 12px;
  }
  .card {
    background: var(--bg2);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 14px 16px;
  }
  .card.wide { grid-column: 1 / -1; }
  .row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 3px 0;
    border-bottom: 1px solid #1c2128;
  }
  .row:last-child { border-bottom: none; }
  .label { color: var(--dim); width: 160px; flex-shrink: 0; }
  .val   { text-align: right; font-weight: bold; }
  .green  { color: var(--green);  }
  .red    { color: var(--red);    }
  .yellow { color: var(--yellow); }
  .cyan   { color: var(--cyan);   }
  .dim    { color: var(--dim);    }
  .orange { color: var(--orange); }
  .purple { color: var(--purple); }

  /* Status badge */
  .badge {
    display: inline-block;
    padding: 2px 10px;
    border-radius: 4px;
    font-size: 11px;
    font-weight: bold;
    letter-spacing: 0.5px;
    text-transform: uppercase;
  }
  .badge-green  { background: #1a3a22; color: var(--green);  border: 1px solid var(--green); }
  .badge-red    { background: #3a1a1a; color: var(--red);    border: 1px solid var(--red);   }
  .badge-yellow { background: #3a2a0a; color: var(--yellow); border: 1px solid var(--yellow);}

  /* Timer bar */
  .timer-wrap { margin-top: 6px; }
  .timer-bar-bg {
    background: var(--bg3);
    border-radius: 4px;
    height: 10px;
    overflow: hidden;
    margin-top: 4px;
  }
  .timer-bar { height: 100%; border-radius: 4px; transition: width 0.4s linear; }

  /* Signal chips */
  .signals { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 4px; }
  .chip {
    background: var(--bg3);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 6px 12px;
    flex: 1 1 140px;
    text-align: center;
  }
  .chip-label { color: var(--dim); font-size: 11px; text-transform: uppercase; }
  .chip-val   { font-size: 16px; font-weight: bold; margin: 2px 0; }
  .chip-note  { font-size: 11px; color: var(--dim); }

  /* Table */
  table { width: 100%; border-collapse: collapse; margin-top: 6px; }
  th { color: var(--dim); font-size: 11px; text-align: left; padding: 4px 6px;
       border-bottom: 1px solid var(--border); text-transform: uppercase; }
  td { padding: 4px 6px; border-bottom: 1px solid #1c2128; font-size: 12px; }
  tr:last-child td { border-bottom: none; }
  tr:hover td { background: var(--bg3); }

  /* Buttons */
  .btns { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 10px; }
  button {
    background: var(--bg3);
    border: 1px solid var(--border);
    color: var(--text);
    border-radius: 6px;
    padding: 7px 18px;
    font-family: inherit;
    font-size: 12px;
    cursor: pointer;
    transition: background 0.15s;
  }
  button:hover { background: var(--border); }
  button.buy-up   { border-color: var(--green);  color: var(--green);  }
  button.buy-down { border-color: var(--red);    color: var(--red);    }
  button.pause    { border-color: var(--yellow); color: var(--yellow); }
  button.resume   { border-color: var(--cyan);   color: var(--cyan);   }

  /* Header strip */
  #header {
    display: flex;
    align-items: center;
    gap: 14px;
    background: var(--bg2);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 10px 16px;
    margin-bottom: 12px;
    flex-wrap: wrap;
  }
  #header .title { color: var(--yellow); font-size: 16px; font-weight: bold; letter-spacing: 1px; }
  #header .spacer { flex: 1; }
  #last-updated { color: var(--dim); font-size: 11px; }
  #status-msg {
    position: fixed; bottom: 14px; right: 18px;
    background: var(--bg3); border: 1px solid var(--border);
    border-radius: 6px; padding: 6px 14px; font-size: 12px;
    color: var(--cyan); display: none;
  }
</style>
</head>
<body>

<div id="header">
  <span class="title">&#8383; POLYMARKET BTC BOT</span>
  <span id="mode-badge" class="badge badge-yellow">PAPER</span>
  <span id="run-badge"  class="badge badge-red">STOPPED</span>
  <span class="spacer"></span>
  <span id="uptime"  class="dim"></span>
  <span id="last-updated" class="dim">—</span>
</div>

<div class="grid">

  <!-- BTC / Prices -->
  <div class="card">
    <h2>&#9728; BTC Price</h2>
    <div class="row"><span class="label" id="btc-src-lbl">Live (…)</span>   <span id="btc-live"   class="val cyan">—</span></div>
    <div class="row"><span class="label">Chainlink (settle)</span><span id="cl-price"  class="val dim">—</span></div>
    <div class="row"><span class="label">HL Oracle</span>         <span id="hl-price"  class="val dim">—</span></div>
    <div class="row"><span class="label">vs Chainlink &#916;</span><span id="cl-delta" class="val">—</span></div>
    <div class="row"><span class="label">1m change</span>         <span id="chg1m"     class="val">—</span></div>
    <div class="row"><span class="label">5m change</span>         <span id="chg5m"     class="val">—</span></div>
    <div class="row"><span class="label">Funding / OI</span>      <span id="funding"   class="val">—</span></div>
    <div class="row"><span class="label">Latency</span>            <span id="latency"  class="val dim">—</span></div>
  </div>

  <!-- Bot / P&L -->
  <div class="card">
    <h2>&#128181; Bot / P&amp;L</h2>
    <div class="row"><span class="label">Capital</span>       <span id="capital"   class="val cyan">—</span></div>
    <div class="row"><span class="label">P&amp;L (today)</span><span id="pnl"     class="val">—</span></div>
    <div class="row"><span class="label">Win / Loss</span>    <span id="wl"        class="val">—</span></div>
    <div class="row"><span class="label">Win Rate</span>      <span id="wr"        class="val">—</span></div>
    <div class="row"><span class="label">Open trades</span>   <span id="open-n"   class="val">—</span></div>
    <div class="row"><span class="label">Consec losses</span> <span id="cons-loss" class="val">—</span></div>
    <div class="row"><span class="label">Claude regime</span> <span id="claude"    class="val dim">—</span></div>
    <div class="row"><span class="label">Markets tracked</span><span id="tracked" class="val">—</span></div>

    <div class="btns">
      <button class="buy-up"   onclick="cmd('buy','YES')" title="B">&#11014; Buy UP [B]</button>
      <button class="buy-down" onclick="cmd('buy','NO')"  title="S">&#11015; Buy DOWN [S]</button>
      <button class="pause"    onclick="cmd('pause')"     title="P">&#9646;&#9646; Pause [P]</button>
      <button class="resume"   onclick="cmd('start')"     title="R">&#9654; Resume [R]</button>
    </div>
  </div>

  <!-- Market / Timer -->
  <div class="card wide">
    <h2>&#128200; Active Market</h2>
    <div id="question" style="color:var(--yellow);font-size:14px;margin-bottom:10px;">Waiting for market…</div>
    <div class="row">
      <span class="label">UP  bid / ask</span>
      <span id="up-book" class="val green">—</span>
    </div>
    <div class="row">
      <span class="label">DOWN  bid / ask</span>
      <span id="dn-book" class="val red">—</span>
    </div>
    <div class="timer-wrap">
      <div class="row">
        <span class="label">Time remaining</span>
        <span id="timer-txt" class="val">—</span>
      </div>
      <div class="timer-bar-bg">
        <div id="timer-bar" class="timer-bar" style="width:100%;background:var(--green)"></div>
      </div>
    </div>
  </div>

  <!-- Signals -->
  <div class="card wide">
    <h2>&#128301; Signals</h2>
    <div class="signals">
      <div class="chip" id="chip-cvd">
        <div class="chip-label">CVD</div>
        <div class="chip-val dim">—</div>
        <div class="chip-note">neutral</div>
      </div>
      <div class="chip" id="chip-liq">
        <div class="chip-label">Liquidations</div>
        <div class="chip-val dim">—</div>
        <div class="chip-note">balanced</div>
      </div>
      <div class="chip" id="chip-fund">
        <div class="chip-label">Funding</div>
        <div class="chip-val dim">—</div>
        <div class="chip-note">neutral</div>
      </div>
      <div class="chip" id="chip-oi">
        <div class="chip-label">Open Interest</div>
        <div class="chip-val dim">—</div>
        <div class="chip-note">flat</div>
      </div>
    </div>
  </div>

  <!-- Open Trades -->
  <div class="card wide" id="trades-card">
    <h2>&#128203; Open Trades</h2>
    <table>
      <thead>
        <tr>
          <th>ID</th><th>Side</th><th>Entry</th><th>Current</th>
          <th>PnL%</th><th>Shares</th><th>Held (s)</th><th>Market</th>
        </tr>
      </thead>
      <tbody id="trades-body"><tr><td colspan="8" class="dim">No open trades</td></tr></tbody>
    </table>
  </div>

  <!-- Active Markets -->
  <div class="card wide" id="markets-card">
    <h2>&#128295; Tracked Markets</h2>
    <table>
      <thead>
        <tr>
          <th>Question</th><th>YES</th><th>NO</th><th>Expiry</th>
          <th>Signal</th><th>Edge%</th><th>Conf</th><th>Pos?</th><th>Status</th>
        </tr>
      </thead>
      <tbody id="markets-body"><tr><td colspan="9" class="dim">No markets tracked</td></tr></tbody>
    </table>
  </div>

</div><!-- /grid -->

<div id="status-msg"></div>

<script>
const $ = id => document.getElementById(id);

function fmt(v, dec=2) {
  if (v === null || v === undefined) return '—';
  return Number(v).toFixed(dec);
}
function fmtMoney(v) {
  return '$' + Number(v).toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2});
}
function signColor(v, inv) {
  if (v === null || v === undefined) return 'dim';
  const pos = (v > 0) !== inv;  // inv=true means positive is bad
  return pos ? 'green' : v < 0 ? 'red' : 'dim';
}
function setEl(id, html, cls) {
  const el = $(id);
  if (!el) return;
  el.innerHTML = html;
  if (cls !== undefined) el.className = 'val ' + cls;
}
function uptime(s) {
  const h = Math.floor(s/3600), m = Math.floor((s%3600)/60), ss = s%60;
  return `${String(h).padStart(2,'0')}:${String(m).padStart(2,'0')}:${String(ss).padStart(2,'0')}`;
}

async function refresh() {
  let d;
  try {
    const r = await fetch('/state');
    if (!r.ok) return;
    d = await r.json();
  } catch(e) { return; }

  // ── Header ──
  const modeBadge = $('mode-badge');
  modeBadge.textContent = (d.mode || 'paper').toUpperCase();
  modeBadge.className   = 'badge ' + (d.mode==='live' ? 'badge-red' : 'badge-yellow');

  const runBadge = $('run-badge');
  const running  = d.running, paused = d.paused;
  if (!running) { runBadge.textContent='STOPPED'; runBadge.className='badge badge-red'; }
  else if (paused) { runBadge.textContent='PAUSED'; runBadge.className='badge badge-yellow'; }
  else { runBadge.textContent='RUNNING'; runBadge.className='badge badge-green'; }

  $('uptime').textContent = 'Up ' + uptime(d.uptime_seconds || 0);
  $('last-updated').textContent = d.last_updated || '—';

  // ── BTC ──
  const btc = d.btc_price || 0;
  const cl  = d.chainlink_price || 0;
  const hl  = d.hl_price || 0;
  setEl('btc-live', btc ? fmtMoney(btc) : 'fetching…', 'val cyan');
  const srcMap = {binance:'Live (Binance)', kraken:'Live (Kraken)', rest:'Live (Kraken REST)'};
  const srcEl = document.getElementById('btc-src-lbl');
  if (srcEl) srcEl.textContent = srcMap[d.btc_source] || 'Live';

  setEl('cl-price', cl ? fmtMoney(cl) : '—', 'val dim');
  setEl('hl-price', hl ? fmtMoney(hl) : '—', 'val dim');

  if (btc > 0 && cl > 0) {
    const delta = (btc - cl) / cl * 100;
    const sign  = delta >= 0 ? '+' : '';
    const lbl   = delta >= 0 ? 'leads ▲' : 'lags ▼';
    const col   = delta >= 0 ? 'green' : 'red';
    setEl('cl-delta', `${sign}${delta.toFixed(3)}%  ${lbl}`, 'val ' + col);
  }

  const chg1 = d.btc_change_1m || 0;
  const chg5 = d.btc_change_5m || 0;
  setEl('chg1m', (chg1>=0?'+':'') + (chg1*100).toFixed(3)+'%', 'val ' + (chg1>=0?'green':'red'));
  setEl('chg5m', (chg5>=0?'+':'') + (chg5*100).toFixed(3)+'%', 'val ' + (chg5>=0?'green':'red'));

  const fund    = d.hl_funding || 0;
  const oi_b    = d.hl_oi_b   || 0;
  const fundCol = fund > 0.02 ? 'red' : fund < -0.02 ? 'green' : 'dim';
  setEl('funding', `${fund>=0?'+':''}${fund.toFixed(4)}%/hr  |  OI $${oi_b.toFixed(2)}B`, 'val ' + fundCol);
  setEl('latency', d.last_latency_ms != null ? d.last_latency_ms + ' ms' : '—', 'val dim');

  // ── P&L ──
  const cap    = d.capital      || 1000;
  const start  = d.start_capital|| 1000;
  const pnl    = cap - start;
  const wins   = d.wins   || 0;
  const losses = d.losses || 0;
  const total  = wins + losses;
  const wr     = total ? (wins / total * 100) : 0;

  setEl('capital',   fmtMoney(cap), 'val cyan');
  setEl('pnl',       (pnl>=0?'+':'') + fmtMoney(pnl), 'val ' + (pnl>=0?'green':'red'));
  setEl('wl',        `W: ${wins}  L: ${losses}`, 'val');
  setEl('wr',        total ? wr.toFixed(1)+'%' : '—', 'val ' + (wr>=55?'green':wr>=45?'yellow':'red'));
  setEl('open-n',    (d.open_trades||[]).length, 'val');
  setEl('cons-loss', d.consecutive_losses || 0, 'val ' + (d.consecutive_losses>=3?'red':'dim'));
  const regime = d.claude_regime || 'unknown';
  const mult   = d.claude_multiplier != null ? ` ×${d.claude_multiplier}` : '';
  setEl('claude',    d.claude_enabled ? regime + mult : 'disabled', 'val dim');
  const mActive = d.markets_tracked != null ? d.markets_tracked : '?';
  const mTotal  = d.markets_total   != null ? d.markets_total   : '?';
  setEl('tracked', `${mActive} active / ${mTotal} total`, 'val');

  // ── Active market (first from list or fallback) ──
  const mkt = (d.active_markets || [])[0];
  if (mkt) {
    $('question').textContent = mkt.question || 'Waiting…';
    const secs = mkt.seconds_to_expiry || 0;
    const pct  = Math.max(0, Math.min(1, secs / 300));
    const mins = Math.floor(secs/60), ss = secs%60;
    $('timer-txt').textContent = `${mins}:${String(ss).padStart(2,'0')}`;
    $('timer-txt').className   = 'val ' + (secs<30?'red':secs<60?'yellow':'green');
    const bar = $('timer-bar');
    bar.style.width = (pct*100).toFixed(1) + '%';
    bar.style.background = secs<30 ? 'var(--red)' : secs<60 ? 'var(--yellow)' : 'var(--green)';
    setEl('up-book', `${fmt(mkt.yes_price,4)}  spread ${fmt(mkt.yes_price,4)}`, 'val green');
    setEl('dn-book', `${fmt(mkt.no_price, 4)}  spread ${fmt(mkt.no_price, 4)}`, 'val red');
  }

  // ── Signals ──
  function updateChip(chipId, val, posLbl, negLbl, neutLbl, inv) {
    const chip = $(chipId);
    if (!chip) return;
    const vEl  = chip.querySelector('.chip-val');
    const nEl  = chip.querySelector('.chip-note');
    vEl.textContent = val != null ? (val >= 0 ? '+' : '') + fmt(val, 2) : '—';
    const col = val == null ? 'dim' : ((val > 0) !== inv) ? 'green' : val < 0 ? 'red' : 'dim';
    vEl.className = 'chip-val ' + col;
    if (val != null) {
      nEl.textContent = val > 0.1 ? posLbl : val < -0.1 ? negLbl : neutLbl;
    }
  }
  updateChip('chip-cvd',  d.cvd_signal,     'bull div',       'bear div',  'neutral', false);
  updateChip('chip-liq',  d.liq_signal,     'short squeeze ▲','long liqs ▼','balanced', false);
  updateChip('chip-fund', d.funding_signal, 'oversold',       'overbought','neutral', false);
  updateChip('chip-oi',   d.oi_signal,      'growing',        'shrinking', 'flat',    false);

  // ── Open trades table ──
  const tbody = $('trades-body');
  const trades = d.open_trades || [];
  if (trades.length === 0) {
    tbody.innerHTML = '<tr><td colspan="8" class="dim">No open trades</td></tr>';
  } else {
    const now = Date.now() / 1000;
    tbody.innerHTML = trades.map(t => {
      const pnlPct = t.entry_price > 0 ? ((t.current_price - t.entry_price) / t.entry_price * 100) : 0;
      const pnlCol = pnlPct >= 0 ? 'green' : 'red';
      const sideCol = t.side === 'YES' ? 'green' : 'red';
      const held = t.entry_time ? Math.round(now - t.entry_time) : '—';
      return `<tr>
        <td class="dim">${(t.trade_id||'').slice(-6)}</td>
        <td class="${sideCol}">${t.side}</td>
        <td>${fmt(t.entry_price,4)}</td>
        <td>${fmt(t.current_price,4)}</td>
        <td class="${pnlCol}">${pnlPct>=0?'+':''}${pnlPct.toFixed(2)}%</td>
        <td>${fmt(t.shares,1)}</td>
        <td class="dim">${held}s</td>
        <td class="dim" style="max-width:200px;overflow:hidden;white-space:nowrap;text-overflow:ellipsis">${t.question||''}</td>
      </tr>`;
    }).join('');
  }

  // ── Markets table — show all tracked markets (active window + pending future ones) ──
  const mbody = $('markets-body');
  // Build a lookup of active-window markets by condition_id prefix for signal/edge/conf
  const activeByKey = {};
  (d.active_markets || []).forEach(m => { activeByKey[m.condition_id] = m; });

  // Merge: tracked_markets_detail gives all found markets; enrich with signal data when available
  const allMkts = d.tracked_markets_detail || d.active_markets || [];
  if (allMkts.length === 0) {
    mbody.innerHTML = '<tr><td colspan="9" class="dim">No markets found — waiting for scan…</td></tr>';
  } else {
    mbody.innerHTML = allMkts.map(m => {
      const active = activeByKey[m.condition_id] || null;
      const secs = m.seconds_to_expiry || 0;
      // Expiry display: MM:SS for markets in/near window; "Xh Xm" for far-future
      let expiryStr, tCol;
      if (secs > 3600) {
        const h = Math.floor(secs/3600), min = Math.floor((secs%3600)/60);
        expiryStr = `${h}h ${min}m`;
        tCol = 'dim';
      } else if (secs > 0) {
        const mins = Math.floor(secs/60), ss = secs%60;
        expiryStr = `${mins}:${String(ss).padStart(2,'0')}`;
        tCol = secs < 30 ? 'red' : secs < 60 ? 'yellow' : 'green';
      } else {
        expiryStr = 'expired';
        tCol = 'red';
      }
      const inWindow = m.in_window;
      const sigCol = active ? (active.signal === 'YES' ? 'green' : active.signal === 'NO' ? 'red' : 'dim') : 'dim';
      const signal  = active ? active.signal : '—';
      const edgePct = active ? `${active.edge_pct >= 0 ? '+' : ''}${fmt(active.edge_pct,2)}%` : '—';
      const conf    = active ? fmt(active.confidence,2) : '—';
      const inPos   = active ? active.in_position : false;
      const statusStr = inWindow ? '<span class="green">ACTIVE</span>' : `<span class="dim">pending</span>`;
      return `<tr ${inPos ? 'style="background:#1a2a1a"' : (inWindow ? '' : 'style="opacity:0.6"')}>
        <td style="max-width:240px;overflow:hidden;white-space:nowrap;text-overflow:ellipsis">${m.question}</td>
        <td class="green">${fmt(m.yes_price,4)}</td>
        <td class="red">${fmt(m.no_price,4)}</td>
        <td class="${tCol}">${expiryStr}</td>
        <td class="${sigCol}">${signal}</td>
        <td>${edgePct}</td>
        <td class="dim">${conf}</td>
        <td>${inPos ? '<span class="green">●</span>' : '<span class="dim">○</span>'}</td>
        <td>${statusStr}</td>
      </tr>`;
    }).join('');
  }
}

// ── Commands ──
async function cmd(action, side) {
  const body = { action, timestamp: Date.now() / 1000 };
  if (side) body.side = side;
  await fetch('/command', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const labels = { buy: side === 'YES' ? '⬆ BUY UP sent' : '⬇ BUY DOWN sent',
                   pause: '⏸ Paused', start: '▶ Resumed' };
  flash(labels[action] || action);
}

function flash(msg) {
  const el = $('status-msg');
  el.textContent = msg;
  el.style.display = 'block';
  clearTimeout(el._t);
  el._t = setTimeout(() => { el.style.display = 'none'; }, 2500);
}

// Keyboard shortcuts
document.addEventListener('keydown', e => {
  if (e.target.tagName === 'INPUT') return;
  const k = e.key.toLowerCase();
  if (k === 'b') cmd('buy', 'YES');
  else if (k === 's') cmd('buy', 'NO');
  else if (k === 'p') cmd('pause');
  else if (k === 'r') cmd('start');
});

setInterval(refresh, 1000);
refresh();
</script>
</body>
</html>"""


# ─────────────────────────────────────────────────────────────────────────────
#  HTTP SERVER
# ─────────────────────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):  # silence default access logs
        pass

    def _send(self, code, content_type, body: bytes):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", HTML.encode())
        elif self.path == "/state":
            try:
                with open(STATE_FILE) as f:
                    data = f.read()
            except FileNotFoundError:
                data = "{}"
            self._send(200, "application/json", data.encode())
        else:
            self._send(404, "text/plain", b"Not found")

    def do_POST(self):
        if self.path == "/command":
            length = int(self.headers.get("Content-Length", 0))
            body   = self.rfile.read(length)
            try:
                cmd = json.loads(body)
                with open(COMMAND_FILE, "w") as f:
                    json.dump(cmd, f)
                self._send(200, "application/json", b'{"ok":true}')
            except Exception as e:
                self._send(400, "application/json",
                           json.dumps({"error": str(e)}).encode())
        else:
            self._send(404, "text/plain", b"Not found")


# ─────────────────────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"  Dashboard running at  http://localhost:{PORT}")
    print(f"  Press Ctrl-C to stop.")

    # Open browser after a short delay (non-blocking)
    def _open():
        time.sleep(0.6)
        webbrowser.open(f"http://localhost:{PORT}")
    threading.Thread(target=_open, daemon=True).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped.")


if __name__ == "__main__":
    main()
