import { useState, useEffect, useRef, useCallback } from "react";

// ═══════════════════════════════════════════════════════════
//  POLYMARKET BTC BOT — TRADING TERMINAL DASHBOARD
//  Reads bot_state.json and displays real-time trading data
// ═══════════════════════════════════════════════════════════

// Color system — trading terminal aesthetic
const C = {
  bg: "#0a0e17",
  panel: "#111827",
  panelBorder: "#1e293b",
  panelHover: "#1a2332",
  green: "#00ff88",
  greenDim: "#00cc6a",
  greenBg: "rgba(0,255,136,0.08)",
  red: "#ff3366",
  redDim: "#cc2952",
  redBg: "rgba(255,51,102,0.08)",
  yellow: "#ffcc00",
  yellowDim: "#ccaa00",
  yellowBg: "rgba(255,204,0,0.06)",
  cyan: "#00d4ff",
  cyanDim: "#00aacc",
  blue: "#4488ff",
  purple: "#aa66ff",
  text: "#e2e8f0",
  textDim: "#64748b",
  textMuted: "#475569",
  white: "#ffffff",
};

// ── Mock data generator for demo mode ──
function generateMockState(tick) {
  const btcBase = 87250 + Math.sin(tick * 0.02) * 800 + Math.random() * 200;
  const capital = 1000 + Math.sin(tick * 0.01) * 15 + tick * 0.05;
  const wins = Math.floor(tick / 3);
  const losses = Math.floor(tick / 5);
  const pnlHistory = Array.from({ length: Math.min(tick, 100) }, (_, i) =>
    Math.sin(i * 0.05) * 5 + i * 0.08 + Math.random() * 2
  );

  const openTrades = tick % 7 < 3 ? [{
    trade_id: `PAPER-${String(tick).padStart(5, "0")}`,
    side: Math.random() > 0.5 ? "YES" : "NO",
    entry_price: 0.52 + Math.random() * 0.04,
    shares: 8 + Math.random() * 4,
    capital_used: 5.0,
    entry_time: Date.now() / 1000 - 60 - Math.random() * 120,
    current_price: 0.52 + Math.random() * 0.06,
    question: "Will BTC go up in the next 5 minutes?",
  }] : [];

  const activeMarkets = [
    {
      condition_id: "0xabc123",
      question: "Will BTC be higher in 5 minutes?",
      yes_price: 0.52 + Math.sin(tick * 0.1) * 0.03,
      no_price: 0.48 - Math.sin(tick * 0.1) * 0.03,
      seconds_to_expiry: 300 - (tick % 300),
      signal: ["YES", "NO", "HOLD"][tick % 3],
      edge_pct: 0.3 + Math.random() * 0.5,
      confidence: 0.4 + Math.random() * 0.4,
      in_position: tick % 7 < 3,
    },
  ];

  return {
    running: true,
    paused: false,
    mode: "paper",
    capital: Math.round(capital * 100) / 100,
    start_capital: 1000,
    btc_price: Math.round(btcBase * 100) / 100,
    btc_change_1m: (Math.sin(tick * 0.05) * 0.002),
    btc_change_5m: (Math.sin(tick * 0.01) * 0.005),
    chainlink_price: Math.round((btcBase - 5 + Math.random() * 10) * 100) / 100,
    hl_price: Math.round((btcBase + Math.random() * 8) * 100) / 100,
    hl_funding: (Math.sin(tick * 0.03) * 0.04),
    hl_oi_b: 4.2 + Math.sin(tick * 0.005) * 0.3,
    cvd_signal: Math.sin(tick * 0.04) * 0.8,
    liq_signal: Math.sin(tick * 0.06) * 0.4,
    funding_signal: Math.sin(tick * 0.02) * 0.3,
    oi_signal: Math.sin(tick * 0.015) * 0.2,
    wins: wins,
    losses: losses,
    open_trades: openTrades,
    active_markets: activeMarkets,
    markets_tracked: 2 + (tick % 3),
    last_latency_ms: 12 + Math.random() * 30,
    scan_count: tick * 10,
    consecutive_losses: tick % 8 < 2 ? 2 : 0,
    claude_enabled: true,
    claude_regime: ["trending_up", "sideways", "volatile", "trending_down"][tick % 4],
    claude_multiplier: [1.3, 0.7, 0.8, 1.2][tick % 4],
    uptime_seconds: tick * 2,
    pnl_history: pnlHistory,
    last_updated: new Date().toISOString(),
  };
}

// ── Formatting helpers ──
function fmt$(v, decimals = 2) {
  return `$${Number(v).toLocaleString("en-US", { minimumFractionDigits: decimals, maximumFractionDigits: decimals })}`;
}

function fmtPct(v, decimals = 2) {
  const sign = v >= 0 ? "+" : "";
  return `${sign}${(v * 100).toFixed(decimals)}%`;
}

function fmtTime(seconds) {
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

// ═══════════════════════════════════════════════════════════
//  COMPONENTS
// ═══════════════════════════════════════════════════════════

function StatusBadge({ running, paused, mode }) {
  const status = !running ? "STOPPED" : paused ? "PAUSED" : "RUNNING";
  const color = !running ? C.red : paused ? C.yellow : C.green;
  const bgColor = !running ? C.redBg : paused ? C.yellowBg : C.greenBg;

  return (
    <div style={{
      display: "flex", alignItems: "center", gap: 10,
      padding: "6px 14px", borderRadius: 8,
      background: bgColor, border: `1px solid ${color}30`,
    }}>
      <div style={{
        width: 8, height: 8, borderRadius: "50%", background: color,
        boxShadow: `0 0 8px ${color}80`,
        animation: running && !paused ? "pulse 2s infinite" : "none",
      }} />
      <span style={{ color, fontWeight: 700, fontSize: 13, letterSpacing: 1 }}>{status}</span>
      <span style={{
        fontSize: 10, color: C.textDim, padding: "2px 6px",
        background: "#1e293b", borderRadius: 4, fontWeight: 600,
      }}>{(mode || "paper").toUpperCase()}</span>
    </div>
  );
}

function MetricCard({ label, value, subValue, color = C.text, icon }) {
  return (
    <div style={{
      background: C.panel, border: `1px solid ${C.panelBorder}`,
      borderRadius: 10, padding: "14px 16px", flex: 1, minWidth: 140,
    }}>
      <div style={{ fontSize: 10, color: C.textDim, textTransform: "uppercase", letterSpacing: 1.5, marginBottom: 6 }}>
        {icon && <span style={{ marginRight: 6 }}>{icon}</span>}{label}
      </div>
      <div style={{ fontSize: 22, fontWeight: 700, color, fontFamily: "'JetBrains Mono', 'Fira Code', monospace" }}>
        {value}
      </div>
      {subValue && <div style={{ fontSize: 11, color: C.textDim, marginTop: 3 }}>{subValue}</div>}
    </div>
  );
}

function SignalGauge({ label, value, min = -1, max = 1 }) {
  const pct = Math.max(0, Math.min(100, ((value - min) / (max - min)) * 100));
  const color = value > 0.2 ? C.green : value < -0.2 ? C.red : C.yellow;
  const labelText = value > 0.3 ? "BULLISH" : value < -0.3 ? "BEARISH" : "NEUTRAL";

  return (
    <div style={{ flex: 1, minWidth: 120 }}>
      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 4 }}>
        <span style={{ fontSize: 10, color: C.textDim, textTransform: "uppercase", letterSpacing: 1 }}>{label}</span>
        <span style={{ fontSize: 10, color, fontWeight: 600 }}>{labelText}</span>
      </div>
      <div style={{
        height: 6, background: "#1e293b", borderRadius: 3, position: "relative", overflow: "hidden",
      }}>
        <div style={{
          position: "absolute", left: 0, top: 0, bottom: 0,
          width: `${pct}%`, borderRadius: 3,
          background: `linear-gradient(90deg, ${C.red}, ${C.yellow} 50%, ${C.green})`,
          transition: "width 0.5s ease",
        }} />
        <div style={{
          position: "absolute", left: `calc(${pct}% - 3px)`, top: -2,
          width: 6, height: 10, background: C.white, borderRadius: 2,
          boxShadow: `0 0 6px ${color}80`,
          transition: "left 0.5s ease",
        }} />
      </div>
      <div style={{ fontSize: 12, color, fontWeight: 600, marginTop: 3, fontFamily: "monospace" }}>
        {value >= 0 ? "+" : ""}{value.toFixed(2)}
      </div>
    </div>
  );
}

function MiniChart({ data, width = "100%", height = 80, color = C.green }) {
  if (!data || data.length < 2) return null;

  const min = Math.min(...data);
  const max = Math.max(...data);
  const range = max - min || 1;

  const points = data.map((v, i) => {
    const x = (i / (data.length - 1)) * 100;
    const y = 100 - ((v - min) / range) * 90 - 5;
    return `${x},${y}`;
  }).join(" ");

  const fillPoints = `0,100 ${points} 100,100`;
  const lastValue = data[data.length - 1];
  const lineColor = lastValue >= 0 ? C.green : C.red;

  return (
    <svg viewBox="0 0 100 100" preserveAspectRatio="none" style={{ width, height, display: "block" }}>
      <defs>
        <linearGradient id="chartFill" x1="0" x2="0" y1="0" y2="1">
          <stop offset="0%" stopColor={lineColor} stopOpacity="0.2" />
          <stop offset="100%" stopColor={lineColor} stopOpacity="0.01" />
        </linearGradient>
      </defs>
      <polygon points={fillPoints} fill="url(#chartFill)" />
      <polyline points={points} fill="none" stroke={lineColor} strokeWidth="1.5" vectorEffect="non-scaling-stroke" />
    </svg>
  );
}

function TradeRow({ trade }) {
  const pnlPct = trade.current_price && trade.entry_price
    ? (trade.current_price - trade.entry_price) / trade.entry_price
    : 0;
  const pnl = pnlPct * trade.capital_used;
  const color = pnl >= 0 ? C.green : C.red;
  const held = Math.floor(Date.now() / 1000 - trade.entry_time);

  return (
    <div style={{
      display: "grid", gridTemplateColumns: "1fr 60px 80px 80px 70px 60px",
      gap: 8, padding: "8px 12px", fontSize: 12,
      borderBottom: `1px solid ${C.panelBorder}`,
      fontFamily: "monospace",
    }}>
      <div style={{ color: C.textDim, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
        {trade.trade_id}
      </div>
      <div style={{ color: trade.side === "YES" ? C.green : C.red, fontWeight: 700 }}>
        {trade.side}
      </div>
      <div style={{ color: C.text }}>{trade.entry_price?.toFixed(4)}</div>
      <div style={{ color }}>{trade.current_price?.toFixed(4)}</div>
      <div style={{ color, fontWeight: 600 }}>
        {pnl >= 0 ? "+" : ""}{(pnlPct * 100).toFixed(2)}%
      </div>
      <div style={{ color: C.textDim }}>{held}s</div>
    </div>
  );
}

function MarketRow({ market }) {
  const sigColor = market.signal === "YES" ? C.green : market.signal === "NO" ? C.red : C.textDim;
  const mins = Math.floor(market.seconds_to_expiry / 60);
  const secs = market.seconds_to_expiry % 60;
  const timerColor = market.seconds_to_expiry < 30 ? C.red : market.seconds_to_expiry < 60 ? C.yellow : C.text;

  return (
    <div style={{
      display: "grid", gridTemplateColumns: "1fr 70px 70px 60px 55px 50px",
      gap: 8, padding: "8px 12px", fontSize: 12,
      borderBottom: `1px solid ${C.panelBorder}`,
      fontFamily: "monospace",
      background: market.in_position ? "rgba(0,212,255,0.04)" : "transparent",
    }}>
      <div style={{ color: C.textDim, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", fontSize: 11 }}>
        {market.question}
      </div>
      <div style={{ color: C.green }}>{market.yes_price?.toFixed(3)}</div>
      <div style={{ color: C.red }}>{market.no_price?.toFixed(3)}</div>
      <div style={{ color: sigColor, fontWeight: 700 }}>{market.signal}</div>
      <div style={{ color: market.edge_pct > 0.5 ? C.green : C.textDim }}>{market.edge_pct?.toFixed(2)}%</div>
      <div style={{ color: timerColor }}>{mins}:{String(Math.floor(secs)).padStart(2, "0")}</div>
    </div>
  );
}

function RegimeBadge({ regime, multiplier }) {
  const regimeMap = {
    trending_up: { icon: "📈", color: C.green, label: "TRENDING UP" },
    trending_down: { icon: "📉", color: C.red, label: "TRENDING DOWN" },
    sideways: { icon: "↔️", color: C.yellow, label: "SIDEWAYS" },
    volatile: { icon: "⚡", color: C.purple, label: "VOLATILE" },
    unknown: { icon: "❓", color: C.textDim, label: "UNKNOWN" },
  };
  const r = regimeMap[regime] || regimeMap.unknown;

  return (
    <div style={{
      display: "inline-flex", alignItems: "center", gap: 8,
      padding: "5px 12px", borderRadius: 6,
      background: `${r.color}10`, border: `1px solid ${r.color}30`,
    }}>
      <span>{r.icon}</span>
      <span style={{ color: r.color, fontSize: 11, fontWeight: 700, letterSpacing: 1 }}>{r.label}</span>
      <span style={{ color: C.textDim, fontSize: 10 }}>×{multiplier?.toFixed(1)}</span>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════
//  MAIN DASHBOARD
// ═══════════════════════════════════════════════════════════

export default function TradingDashboard() {
  const [state, setState] = useState(null);
  const [tick, setTick] = useState(0);
  const [tab, setTab] = useState("signals");

  // Simulate real-time updates (in production, this reads bot_state.json)
  useEffect(() => {
    const interval = setInterval(() => {
      setTick(t => {
        const newTick = t + 1;
        setState(generateMockState(newTick));
        return newTick;
      });
    }, 1000);
    return () => clearInterval(interval);
  }, []);

  if (!state) return (
    <div style={{ background: C.bg, height: "100vh", display: "flex", alignItems: "center", justifyContent: "center" }}>
      <div style={{ color: C.textDim, fontSize: 14 }}>Connecting to bot...</div>
    </div>
  );

  const pnl = state.capital - state.start_capital;
  const pnlPct = pnl / state.start_capital;
  const pnlColor = pnl >= 0 ? C.green : C.red;
  const winRate = state.wins + state.losses > 0 ? state.wins / (state.wins + state.losses) : 0;
  const wrColor = winRate >= 0.52 ? C.green : winRate >= 0.45 ? C.yellow : C.red;

  const tabStyle = (active) => ({
    padding: "6px 14px", fontSize: 11, fontWeight: 600, letterSpacing: 0.5,
    border: "none", borderRadius: 6, cursor: "pointer",
    background: active ? C.cyan + "20" : "transparent",
    color: active ? C.cyan : C.textDim,
    transition: "all 0.2s",
  });

  return (
    <div style={{
      background: C.bg, minHeight: "100vh", color: C.text,
      fontFamily: "'Inter', 'SF Pro Display', -apple-system, sans-serif",
      padding: 16, maxWidth: 1200, margin: "0 auto",
    }}>
      <style>{`
        @keyframes pulse { 0%,100% { opacity:1 } 50% { opacity:0.5 } }
        @keyframes slideIn { from { opacity:0; transform:translateY(8px) } to { opacity:1; transform:translateY(0) } }
        * { box-sizing: border-box; }
        ::-webkit-scrollbar { width: 4px; }
        ::-webkit-scrollbar-track { background: ${C.bg}; }
        ::-webkit-scrollbar-thumb { background: ${C.panelBorder}; border-radius: 2px; }
      `}</style>

      {/* ── HEADER ── */}
      <div style={{
        display: "flex", justifyContent: "space-between", alignItems: "center",
        marginBottom: 16, paddingBottom: 12, borderBottom: `1px solid ${C.panelBorder}`,
      }}>
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <div style={{ fontSize: 18, fontWeight: 800, letterSpacing: -0.5 }}>
            <span style={{ color: C.cyan }}>POLY</span>
            <span style={{ color: C.text }}>BOT</span>
          </div>
          <StatusBadge running={state.running} paused={state.paused} mode={state.mode} />
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 16, fontSize: 11, color: C.textDim }}>
          <span>Uptime {fmtTime(state.uptime_seconds)}</span>
          <span>Latency {state.last_latency_ms?.toFixed(0)}ms</span>
          <span>Scans {state.scan_count?.toLocaleString()}</span>
          {state.claude_enabled && <RegimeBadge regime={state.claude_regime} multiplier={state.claude_multiplier} />}
        </div>
      </div>

      {/* ── TOP METRICS ROW ── */}
      <div style={{ display: "flex", gap: 10, marginBottom: 12, flexWrap: "wrap" }}>
        <MetricCard
          label="BTC PRICE" icon="₿"
          value={fmt$(state.btc_price, 2)}
          subValue={`1m: ${fmtPct(state.btc_change_1m)} | 5m: ${fmtPct(state.btc_change_5m)}`}
          color={C.cyan}
        />
        <MetricCard
          label="CAPITAL" icon="💰"
          value={fmt$(state.capital)}
          subValue={`P&L: ${pnl >= 0 ? "+" : ""}${fmt$(pnl)} (${fmtPct(pnlPct)})`}
          color={pnlColor}
        />
        <MetricCard
          label="WIN RATE" icon="🎯"
          value={`${(winRate * 100).toFixed(1)}%`}
          subValue={`${state.wins}W / ${state.losses}L`}
          color={wrColor}
        />
        <MetricCard
          label="POSITIONS" icon="📊"
          value={`${state.open_trades?.length || 0} / 3`}
          subValue={`${state.markets_tracked || 0} markets tracked`}
          color={C.blue}
        />
      </div>

      {/* ── PRICE FEEDS ROW ── */}
      <div style={{
        display: "grid", gridTemplateColumns: "1fr 1fr 1fr",
        gap: 10, marginBottom: 12,
      }}>
        {[
          { label: "BINANCE", price: state.btc_price, color: C.cyan },
          { label: "CHAINLINK (settle)", price: state.chainlink_price, color: C.yellow },
          { label: "HYPERLIQUID", price: state.hl_price, color: C.purple },
        ].map(feed => (
          <div key={feed.label} style={{
            background: C.panel, border: `1px solid ${C.panelBorder}`,
            borderRadius: 8, padding: "10px 14px",
            display: "flex", justifyContent: "space-between", alignItems: "center",
          }}>
            <span style={{ fontSize: 10, color: C.textDim, letterSpacing: 1 }}>{feed.label}</span>
            <span style={{ fontSize: 15, fontWeight: 700, color: feed.color, fontFamily: "monospace" }}>
              {feed.price > 0 ? fmt$(feed.price, 2) : "—"}
            </span>
          </div>
        ))}
      </div>

      {/* ── SIGNAL GAUGES ── */}
      <div style={{
        background: C.panel, border: `1px solid ${C.panelBorder}`,
        borderRadius: 10, padding: 16, marginBottom: 12,
      }}>
        <div style={{ fontSize: 10, color: C.textDim, letterSpacing: 1.5, marginBottom: 12, textTransform: "uppercase" }}>
          Signal Components
        </div>
        <div style={{ display: "flex", gap: 20, flexWrap: "wrap" }}>
          <SignalGauge label="CVD (Volume Delta)" value={state.cvd_signal} />
          <SignalGauge label="Liquidation Pressure" value={state.liq_signal} />
          <SignalGauge label="Funding Rate Signal" value={state.funding_signal} />
          <SignalGauge label="Open Interest Delta" value={state.oi_signal} />
        </div>
        <div style={{ display: "flex", gap: 16, marginTop: 12, fontSize: 11, color: C.textDim }}>
          <span>Funding: {(state.hl_funding || 0).toFixed(4)}%/hr</span>
          <span>OI: ${(state.hl_oi_b || 0).toFixed(2)}B</span>
        </div>
      </div>

      {/* ── P&L CHART ── */}
      <div style={{
        background: C.panel, border: `1px solid ${C.panelBorder}`,
        borderRadius: 10, padding: 16, marginBottom: 12,
      }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
          <span style={{ fontSize: 10, color: C.textDim, letterSpacing: 1.5, textTransform: "uppercase" }}>
            Cumulative P&L
          </span>
          <span style={{ fontSize: 14, fontWeight: 700, color: pnlColor, fontFamily: "monospace" }}>
            {pnl >= 0 ? "+" : ""}{fmt$(pnl)}
          </span>
        </div>
        <MiniChart data={state.pnl_history} height={100} />
      </div>

      {/* ── TABBED CONTENT ── */}
      <div style={{
        background: C.panel, border: `1px solid ${C.panelBorder}`,
        borderRadius: 10, overflow: "hidden",
      }}>
        <div style={{
          display: "flex", gap: 4, padding: "8px 12px",
          borderBottom: `1px solid ${C.panelBorder}`,
        }}>
          {[
            { id: "signals", label: "Active Markets" },
            { id: "positions", label: "Open Positions" },
            { id: "risk", label: "Risk Dashboard" },
          ].map(t => (
            <button key={t.id} onClick={() => setTab(t.id)} style={tabStyle(tab === t.id)}>
              {t.label}
            </button>
          ))}
        </div>

        <div style={{ padding: 0 }}>
          {/* ── ACTIVE MARKETS TAB ── */}
          {tab === "signals" && (
            <div>
              <div style={{
                display: "grid", gridTemplateColumns: "1fr 70px 70px 60px 55px 50px",
                gap: 8, padding: "8px 12px", fontSize: 10, color: C.textDim,
                borderBottom: `1px solid ${C.panelBorder}`, letterSpacing: 0.5,
                textTransform: "uppercase",
              }}>
                <div>Market</div><div>YES</div><div>NO</div><div>Signal</div><div>Edge</div><div>Timer</div>
              </div>
              {(state.active_markets || []).length === 0 ? (
                <div style={{ padding: 24, textAlign: "center", color: C.textDim, fontSize: 13 }}>
                  No active BTC markets found — scanning...
                </div>
              ) : (
                state.active_markets.map((m, i) => <MarketRow key={i} market={m} />)
              )}
            </div>
          )}

          {/* ── OPEN POSITIONS TAB ── */}
          {tab === "positions" && (
            <div>
              <div style={{
                display: "grid", gridTemplateColumns: "1fr 60px 80px 80px 70px 60px",
                gap: 8, padding: "8px 12px", fontSize: 10, color: C.textDim,
                borderBottom: `1px solid ${C.panelBorder}`, letterSpacing: 0.5,
                textTransform: "uppercase",
              }}>
                <div>Trade</div><div>Side</div><div>Entry</div><div>Current</div><div>P&L</div><div>Held</div>
              </div>
              {(state.open_trades || []).length === 0 ? (
                <div style={{ padding: 24, textAlign: "center", color: C.textDim, fontSize: 13 }}>
                  No open positions
                </div>
              ) : (
                state.open_trades.map((t, i) => <TradeRow key={i} trade={t} />)
              )}
            </div>
          )}

          {/* ── RISK DASHBOARD TAB ── */}
          {tab === "risk" && (
            <div style={{ padding: 16, display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16 }}>
              {/* Daily P&L meter */}
              <div>
                <div style={{ fontSize: 10, color: C.textDim, letterSpacing: 1.5, marginBottom: 8, textTransform: "uppercase" }}>
                  Daily Loss Used
                </div>
                {(() => {
                  const dailyPnlPct = pnlPct;
                  const limit = 0.10;
                  const used = Math.min(Math.abs(Math.min(dailyPnlPct, 0)) / limit, 1);
                  const barColor = used > 0.8 ? C.red : used > 0.5 ? C.yellow : C.green;
                  return (
                    <>
                      <div style={{ height: 8, background: "#1e293b", borderRadius: 4, overflow: "hidden" }}>
                        <div style={{
                          height: "100%", width: `${used * 100}%`, borderRadius: 4,
                          background: barColor, transition: "width 0.5s",
                        }} />
                      </div>
                      <div style={{ fontSize: 11, color: barColor, marginTop: 4 }}>
                        {(used * 100).toFixed(1)}% of 10% daily limit
                      </div>
                    </>
                  );
                })()}
              </div>

              {/* Consecutive losses */}
              <div>
                <div style={{ fontSize: 10, color: C.textDim, letterSpacing: 1.5, marginBottom: 8, textTransform: "uppercase" }}>
                  Consecutive Losses
                </div>
                <div style={{ display: "flex", gap: 4 }}>
                  {Array.from({ length: 5 }).map((_, i) => (
                    <div key={i} style={{
                      width: 24, height: 24, borderRadius: 4,
                      background: i < (state.consecutive_losses || 0) ? C.redBg : "#1e293b",
                      border: `1px solid ${i < (state.consecutive_losses || 0) ? C.red + "40" : C.panelBorder}`,
                      display: "flex", alignItems: "center", justifyContent: "center",
                      fontSize: 10, color: i < (state.consecutive_losses || 0) ? C.red : C.textMuted,
                    }}>
                      {i + 1}
                    </div>
                  ))}
                  <span style={{ fontSize: 10, color: C.textDim, alignSelf: "center", marginLeft: 8 }}>
                    breaker @ 5
                  </span>
                </div>
              </div>

              {/* Position slots */}
              <div>
                <div style={{ fontSize: 10, color: C.textDim, letterSpacing: 1.5, marginBottom: 8, textTransform: "uppercase" }}>
                  Position Slots
                </div>
                <div style={{ display: "flex", gap: 4 }}>
                  {Array.from({ length: 3 }).map((_, i) => (
                    <div key={i} style={{
                      width: 36, height: 36, borderRadius: 6,
                      background: i < (state.open_trades?.length || 0) ? C.cyanDim + "15" : "#1e293b",
                      border: `1px solid ${i < (state.open_trades?.length || 0) ? C.cyan + "40" : C.panelBorder}`,
                      display: "flex", alignItems: "center", justifyContent: "center",
                      fontSize: 14, color: i < (state.open_trades?.length || 0) ? C.cyan : C.textMuted,
                    }}>
                      {i < (state.open_trades?.length || 0) ? "●" : "○"}
                    </div>
                  ))}
                </div>
              </div>

              {/* Win rate gauge */}
              <div>
                <div style={{ fontSize: 10, color: C.textDim, letterSpacing: 1.5, marginBottom: 8, textTransform: "uppercase" }}>
                  Session Win Rate
                </div>
                <div style={{ position: "relative" }}>
                  <svg viewBox="0 0 100 50" style={{ width: "100%", maxWidth: 180 }}>
                    {/* Background arc */}
                    <path d="M 10 45 A 40 40 0 0 1 90 45" fill="none" stroke="#1e293b" strokeWidth="6" strokeLinecap="round" />
                    {/* Filled arc (proportional to win rate) */}
                    <path
                      d="M 10 45 A 40 40 0 0 1 90 45"
                      fill="none"
                      stroke={wrColor}
                      strokeWidth="6"
                      strokeLinecap="round"
                      strokeDasharray={`${winRate * 126} 126`}
                      style={{ transition: "stroke-dasharray 0.8s ease" }}
                    />
                    <text x="50" y="42" textAnchor="middle" fill={wrColor} fontSize="14" fontWeight="700" fontFamily="monospace">
                      {(winRate * 100).toFixed(1)}%
                    </text>
                  </svg>
                </div>
              </div>
            </div>
          )}
        </div>
      </div>

      {/* ── FOOTER ── */}
      <div style={{
        display: "flex", justifyContent: "space-between", alignItems: "center",
        marginTop: 12, padding: "8px 0", fontSize: 10, color: C.textMuted,
      }}>
        <span>Polymarket BTC Bot v2.0 — Dashboard</span>
        <span>Last update: {state.last_updated}</span>
      </div>
    </div>
  );
}
