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

Output:
  score: 0–100
  tier:  FULL / REDUCED / CAUTIOUS / PAUSED
  size_multiplier: 1.0 / 0.75 / 0.5 / 0.0
  spread_multiplier: 1.0 / 1.2 / 1.5 / —
  reason: human-readable explanation
"""

import time
from dataclasses import dataclass

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

    def __init__(self, max_inventory: float = 500.0):
        self.max_inventory = max_inventory

    def score(
        self,
        snapshot,                          # SideDataSnapshot
        net_inventory: float,
        feed_timestamps: dict,             # {"binance": ts, "hyperliquid": ts, "gamma": ts}
        market_yes_price: float = 0.50,
    ) -> ConfidenceResult:

        sa  = self._signal_agreement(snapshot)
        df  = self._data_freshness(feed_timestamps)
        sh  = self._spread_health(snapshot, market_yes_price)
        inv = self._inventory_neutral(net_inventory)

        raw = sa * 0.35 + df * 0.30 + sh * 0.20 + inv * 0.15
        score = round(max(0.0, min(100.0, raw)), 1)

        tier, size_mult, spread_mult = self._classify(score)
        reason = self._reason(sa, df, sh, inv)

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
        )

    # ── Factor calculations ───────────────────────────────────────────────────

    def _signal_agreement(self, snapshot) -> float:
        """
        Measures how coherent the four signals are.

        Each signal is in [-1, +1]. Agreement is high when they all
        point the same direction OR are all near zero (calm market).
        Disagreement (strong opposing signals) lowers confidence.
        """
        signals = [
            snapshot.cvd_signal,
            snapshot.funding_signal,
            snapshot.liq_signal,
            snapshot.oi_signal,
        ]

        # Average absolute magnitude — tells us if signals are active at all
        avg_magnitude = sum(abs(s) for s in signals) / len(signals)

        if avg_magnitude < 0.05:
            # All signals near zero = calm market, high agreement
            return 90.0

        # Std dev of signals relative to their magnitude = disagreement
        mean = sum(signals) / len(signals)
        variance = sum((s - mean) ** 2 for s in signals) / len(signals)
        std_dev = variance ** 0.5

        # Normalize: low std_dev relative to magnitude = agreement
        disagreement_ratio = std_dev / (avg_magnitude + 0.01)
        agreement = max(0.0, 100.0 - disagreement_ratio * 100.0)
        return round(agreement, 1)

    def _data_freshness(self, feed_timestamps: dict) -> float:
        """
        All three feeds must have updated recently.

        Binance:      stale after 10s  (WS should tick every ~100ms)
        Hyperliquid:  stale after 15s  (REST polls every 3s)
        Gamma:        stale after 45s  (REST polls every 10s, slower API)
        """
        now = time.time()
        thresholds = {
            "binance":      10.0,
            "hyperliquid":  15.0,
            "gamma":        45.0,
        }
        weights = {
            "binance":      0.40,
            "hyperliquid":  0.40,
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

    def _reason(self, sa, df, sh, inv) -> str:
        issues = []
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
