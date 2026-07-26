"""
Size-aware fill model. A backtest that assumes you always fill at the printed
price overstates a seller's edge — real fills cross the spread, and at ₹10 Cr
size you also move the book. This models both as a haircut applied against you.

Two components:
  1. Spread cost: you buy at ask, sell at bid — model as a fraction of price.
  2. Impact: grows with how many lots you push relative to a liquidity anchor.

Applied so it ALWAYS hurts: entry credit is reduced, exit cost is increased.
Keep it conservative; tune against live micro-fills in Phase 2.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SlippageModel:
    """Impact follows the square-root law, NOT a per-lot linear term.

    A linear per-lot charge is wrong and dangerously so: it makes any size look
    catastrophic (250 lots -> 12% per leg) even when that size is ~1% of a book
    trading 21,500 lots/day. Market impact empirically scales with the SQUARE ROOT
    of participation (your order / average daily volume), so doubling size costs
    ~1.4x, not 2x.

    `impact_coef` is a genuine unknown until calibrated against real fills in
    Phase 2 — treat conclusions that hinge on it as provisional.
    """
    spread_pct: float = 0.01        # 1% of leg price crossed per fill (index opts, liquid)
    impact_coef: float = 0.30       # impact at 100% participation (calibrate!)
    adv_lots: float = 21_500.0      # measured NIFTY weekly OTM-leg depth, lots/day
    min_ticks: float = 0.05         # floor: at least this absolute rupee slip per unit price

    def fill_price(self, mid: float, action: str, lots: int) -> float:
        """Return the realistic fill price for `action` (BUY/SELL) at `lots` size."""
        participation = max(0.0, lots / self.adv_lots)
        slip_frac = self.spread_pct + self.impact_coef * participation ** 0.5
        slip = max(mid * slip_frac, self.min_ticks)
        if action.upper() == "BUY":
            return mid + slip          # pay up
        return max(0.05, mid - slip)   # receive less
