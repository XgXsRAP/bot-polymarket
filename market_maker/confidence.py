"""
Bot confidence scoring system.

Confidence is a composite 0–100 score answering: "How much should
the bot trust its own quotes right now?" It drives quote sizing and
spread widening automatically — no manual intervention needed.

Four factors, each 0–100, weighted into a final score:

  Signal Agreement   (35%) — do CVD, funding, liq, OI agree?
  Data Freshness     (30%) — are all feeds live and recent?
  Spread Health      (20%) — is there real profit margin above fees?
  Inventory Neutral  (15%) — how close to flat is inventory?

Plus a volatility regime penalty applied after the weighted sum:

  Volatility Penalty (−25 max) — subtracted when btc_volatility_1m
                                  spikes above 2× its 15-min rolling avg

Output:
  score: 0–100
  tier:  FULL / REDUCED / CAUTIOUS / PAUSED
  size_multiplier: 1.0 / 0.75 / 0.5 / 0.0
  spread_multiplier: 1.0 / 1.2 / 1.5 / —
  reason: human-readable explanation
"""

import time
from collections import deque
from dataclasses import dataclass, field

from fees import minimum_profitable_spread


# ── Confidence tiers ─────────────────────────────────────────────────────────

@dataclass
class ConfidenceResult:
    score: float                  # 0–100
    tier: str                     # FULL / REDUCED / CAUTIOUS / PAUSED
    size_multiplier: float        # Applied to base quote size
    spread_multiplier: float      # Applied to base spread
    reason: str                   # Short human-readable explanation

    # Factor breakdown for dashboard display
    signal_agreement: float       # 0–100
    data_freshness: float         # 0–100
    spread_health: float          # 0–100
    inventory_neutral: float      # 0–100
    volatility_penalty: float = 0.0     # Points subtracted for vol spike (0–25)
    loss_streak_penalty: float = 0.0    # Points subtracted for consecutive losses (0–40)
    model_accuracy_penalty: float = 0.0 # Points subtracted for systematic FV bias (0–15)


_TIERS = [
    (80, "FULL",     1.00, 1.00),
    (60, "REDUCED",  0.75, 1.20),
    (40, "CAUTIOUS", 0.50, 1.50),
    (0,  "PAUSED",   0.00, 2.00),
]


# ── Calculator ────────────────────────────────────────────────────────────────

class ConfidenceCalculator:
    """
    Computes a confidence score from live snapshot data.

    Usage:
        calc = ConfidenceCalculator(max_inventory=500)
        result = calc.score(snapshot, net_inventory, feed_timestamps)
        print(result.score, result.tier, result.reason)
    """

    # 900 samples at 1 s/cycle = 15 minutes of rolling vol history
    _VOL_WINDOW = 900
    # Minimum samples before the penalty activates (5 minutes)
    _VOL_MIN_SAMPLES = 300

    def __init__(self, max_inventory: float = 500.0):
        self.max_inventory = max_inventory
        self._vol_history: deque[float] = deque(maxlen=self._VOL_WINDOW)
        self._fv_errors: deque[float] = deque(maxlen=20)  # predicted − actual_mid

    def score(
        self,
        snapshot,                          # SideDataSnapshot
        net_inventory: float,
        feed_timestamps: dict,             # {"binance": ts, "hyperliquid": ts, "gamma": ts, "chainlink": ts}
        market_yes_price: float = 0.50,
        consecutive_losses: int = 0,
    ) -> ConfidenceResult:

        # Record volatility sample for rolling window
        if snapshot.btc_volatility_1m:
            self._vol_history.append(snapshot.btc_volatility_1m)

        # Record FV prediction error vs live market mid
        actual_mid = (snapshot.market_best_bid + snapshot.market_best_ask) / 2
        if actual_mid > 0:
            self._fv_errors.append(market_yes_price - actual_mid)

        sa   = self._signal_agreement(snapshot)
        df   = self._data_freshness(feed_timestamps)
        sh   = self._spread_health(snapshot, market_yes_price)
        inv  = self._inventory_neutral(net_inventory)
        vp   = self._volatility_penalty(snapshot)
        lsp  = self._loss_streak_penalty(consecutive_losses)
        map_ = self._model_accuracy_penalty()

        raw = sa * 0.35 + df * 0.30 + sh * 0.20 + inv * 0.15
        score = round(max(0.0, min(100.0, raw - vp - lsp - map_)), 1)

        # Hard cap: 5+ consecutive losses forces CAUTIOUS regardless of other factors
        if consecutive_losses >= 5:
            score = min(score, 59.0)

        tier, size_mult, spread_mult = self._classify(score)
        reason = self._reason(sa, df, sh, inv, vp, lsp, map_)

        return ConfidenceResult(
            score=score,
            tier=tier,
            size_multiplier=size_mult,
            spread_multiplier=spread_mult,
            reason=reason,
            signal_agreement=sa,
            data_freshness=df,
            spread_health=sh,
            inventory_neutral=inv,
            volatility_penalty=vp,
            loss_streak_penalty=lsp,
            model_accuracy_penalty=map_,
        )

    # ── Factor calculations ───────────────────────────────────────────────────

    def _signal_agreement(self, snapshot) -> float:
        """
        Measures how coherently the signals point in one direction.

        Uses weighted directional consensus: signals are weighted by their
        magnitude, then we measure how much they net-agree on direction.

        Examples:
          CVD=+0.5, liq=+0.4, OI=+0.3, funding=-0.1 → net=+1.1/1.3=+0.85 → 85%
          CVD=+0.5, funding=-0.5, liq=0, OI=0       → net=0/1.0=0.0     →  0%
          all signals < 0.05                          → calm market       → 85%

        Also incorporates BTC momentum (1m and 5m) as a fifth signal,
        normalized to [-1, +1] using 0.5% as the saturation reference.
        """
        # Core derivative signals
        signals = [
            snapshot.cvd_signal,
            snapshot.funding_signal,
            snapshot.liq_signal,
            snapshot.oi_signal,
        ]

        # Add BTC momentum signals if available — normalize 0.5% move → ±1.0
        if snapshot.btc_change_1m:
            signals.append(max(-1.0, min(1.0, snapshot.btc_change_1m / 0.005)))
        if snapshot.btc_change_5m:
            signals.append(max(-1.0, min(1.0, snapshot.btc_change_5m / 0.01)))

        total_weight = sum(abs(s) for s in signals)

        if total_weight < 0.10:
            # All signals near zero = calm market, safe to quote
            return 85.0

        # Directional consensus: +1 = fully bullish, -1 = fully bearish, 0 = split
        net = sum(signals)
        consensus = abs(net) / total_weight  # 0.0 to 1.0
        return round(consensus * 100.0, 1)

    def _data_freshness(self, feed_timestamps: dict) -> float:
        """
        All four feeds must have updated recently.

        Binance:     stale after 10s  (WS ticks every ~100ms)
        Hyperliquid: stale after 15s  (REST polls every 3s)
        Chainlink:   stale after 30s  (on-chain; updates every ~27s or on 0.5% BTC move)
        Gamma:       stale after 45s  (REST polls every 10s, slower API)

        Chainlink carries the highest weight (0.30) because it is the
        settlement reference — a stale Chainlink anchors fair value to
        a wrong price.
        """
        now = time.time()
        thresholds = {
            "binance":      10.0,   # WS / Kraken fallback ticks every ~100ms
            "hyperliquid":  15.0,   # REST polls every 3s
            "chainlink":    30.0,   # On-chain; updates every ~27s or on 0.5% BTC move
            "gamma":        45.0,   # REST polls every 10s
        }
        weights = {
            "binance":      0.25,
            "hyperliquid":  0.25,
            "chainlink":    0.30,   # Settlement source — highest single weight
            "gamma":        0.20,
        }

        total_weight = 0.0
        weighted_score = 0.0

        for feed, max_age in thresholds.items():
            ts = feed_timestamps.get(feed, 0.0)
            age = now - ts if ts > 0 else float("inf")
            w = weights[feed]

            if age <= max_age:
                feed_score = 100.0
            elif age <= max_age * 3:
                # Linear degradation to 0 over 3× the threshold
                feed_score = 100.0 * (1 - (age - max_age) / (max_age * 2))
            else:
                feed_score = 0.0

            weighted_score += w * max(0.0, feed_score)
            total_weight += w

        return round(weighted_score / total_weight, 1) if total_weight > 0 else 0.0

    def _spread_health(self, snapshot, market_yes_price: float) -> float:
        """
        How much margin does the current spread have above the fee floor?

        Uses the minimum_profitable_spread from fees.py to compute
        what's the bare minimum we need. Then measures how much headroom
        we have above that.
        """
        try:
            min_spread = minimum_profitable_spread(market_yes_price)
        except Exception:
            min_spread = 0.018  # Safe default for crypto at p=0.50

        # How wide can we realistically quote given current conditions?
        # Use the market spread from Gamma as a proxy for how tight the
        # market is. If the market is already very tight, we can't earn.
        current_market_spread = max(snapshot.market_spread, 0.001)

        # Health = how much of current_market_spread is profit above fees
        # A spread exactly at min_spread = 0% health. 2× min_spread = 100%.
        if current_market_spread <= min_spread:
            return 0.0

        ratio = (current_market_spread - min_spread) / min_spread
        return round(min(100.0, ratio * 100.0), 1)

    def _loss_streak_penalty(self, consecutive_losses: int) -> float:
        """
        Subtracts 10 points per loss beyond the 2nd, up to 40 points.
        At 5+ losses, score() also hard-caps to CAUTIOUS (≤59) regardless
        of this value.

          0–2 losses  →  0 pts
          3 losses    → 10 pts
          4 losses    → 20 pts
          5 losses    → 30 pts  (+ CAUTIOUS cap in score())
          6+ losses   → 40 pts  (+ CAUTIOUS cap in score())
        """
        if consecutive_losses < 3:
            return 0.0
        return min(40.0, (consecutive_losses - 2) * 10.0)

    def _model_accuracy_penalty(self) -> float:
        """
        Degrades confidence when the fair value model is systematically biased
        vs the live Gamma market mid-price over the last 20 cycles.

        Requires at least 10 observations before activating to avoid false
        positives at startup or after a market window change.

        Penalty scale (based on |mean bias|):
          < 2¢  →  0 pts  (within noise)
          2–5¢  →  0–15 pts (linear)
          ≥ 5¢  → 15 pts (capped)
        """
        if len(self._fv_errors) < 10:
            return 0.0
        mean_bias = sum(self._fv_errors) / len(self._fv_errors)
        abs_bias = abs(mean_bias)
        if abs_bias < 0.02:
            return 0.0
        elif abs_bias < 0.05:
            return round((abs_bias - 0.02) / 0.03 * 15.0, 1)
        else:
            return 15.0

    def _volatility_penalty(self, snapshot) -> float:
        """
        Subtracts 0–25 points when btc_volatility_1m spikes above 2×
        its 15-minute rolling average — signals a regime change where
        adverse selection risk is high and fair value is unreliable.

        Penalty scale:
          < 2× rolling avg  →  0 pts  (normal)
            2× rolling avg  → 15 pts  (elevated — reduce position)
            3× rolling avg  → 25 pts  (spike — near-pause)
          > 3× rolling avg  → 25 pts  (capped)

        Requires at least 5 minutes of history (300 samples) before
        activating to avoid false positives at startup.
        """
        vol_now = snapshot.btc_volatility_1m
        if not vol_now or len(self._vol_history) < self._VOL_MIN_SAMPLES:
            return 0.0

        vol_avg = sum(self._vol_history) / len(self._vol_history)
        if vol_avg <= 0:
            return 0.0

        ratio = vol_now / vol_avg
        if ratio < 2.0:
            return 0.0
        elif ratio < 3.0:
            # Linear interpolation: 15 pts at 2×, 25 pts at 3×
            return round(15.0 + (ratio - 2.0) * 10.0, 1)
        else:
            return 25.0

    def _inventory_neutral(self, net_inventory: float) -> float:
        """
        How close to flat is the current inventory?

        0 inventory = 100%, max_inventory = 0%.
        Penalizes hard once above 75% of max.
        """
        if self.max_inventory <= 0:
            return 100.0
        ratio = abs(net_inventory) / self.max_inventory
        if ratio >= 1.0:
            return 0.0
        if ratio >= 0.75:
            # Steep penalty above 75%
            return round(max(0.0, (1.0 - ratio) / 0.25 * 30.0), 1)
        return round((1.0 - ratio) * 100.0, 1)

    # ── Classification ────────────────────────────────────────────────────────

    def _classify(self, score: float):
        for threshold, tier, size_mult, spread_mult in _TIERS:
            if score >= threshold:
                return tier, size_mult, spread_mult
        return "PAUSED", 0.0, 2.0

    def _reason(self, sa, df, sh, inv, vol_penalty=0.0, lsp=0.0, map_=0.0) -> str:
        issues = []
        if vol_penalty >= 15:
            issues.append("volatility spike")
        if lsp >= 10:
            issues.append("loss streak")
        if map_ >= 5:
            issues.append("model drift")
        if sa < 50:
            issues.append("signals conflicting")
        if df < 70:
            issues.append("feed data stale")
        if sh < 40:
            issues.append("spread too tight to profit")
        if inv < 40:
            issues.append("inventory overexposed")
        if not issues:
            return "all systems healthy"
        return ", ".join(issues)
