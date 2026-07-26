"""
Strategy selector — "which strategy for today/this week?"

Phase-0: transparent rules (white-box, easy to trust with real money). It scores
each eligible strategy for the regime and returns the best, or None (cash).
Replace the rule table with a learned/bandit policy ONLY after 12+ months of
backtest + forward paper data validate it — never start with ML (you'll overfit).

The core seller's signal is the Variance Risk Premium (VRP): expected move
(implied, via ATM straddle) minus realized move. Sell more when implied > realized.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..strategies.base import Regime, Strategy


@dataclass
class Choice:
    strategy: Strategy | None
    reason: str
    confidence: float  # 0..1, used later for sizing


def select(regime: Regime, library: list[Strategy]) -> Choice:
    if regime.is_event_day:
        return Choice(None, "event day — stand down (expiry-gamma / macro)", 0.0)

    vrp = regime.extras.get("vrp") if regime.extras else None
    eligible = [s for s in library if s.eligible(regime)]
    if not eligible:
        return Choice(None, "no eligible strategy for regime", 0.0)

    # Prefer selling when vol is rich (positive VRP). Confidence scales with VRP.
    if vrp is not None:
        if vrp <= 0:
            return Choice(None, f"implied<=realized (VRP={vrp:.4f}) — no edge to sell, stay flat", 0.0)
        confidence = min(1.0, vrp / 0.01)  # 1% VRP -> full confidence
    else:
        confidence = 0.5  # unknown VRP (bhavcopy-only) — take a middle stance

    # Simple preference: first eligible (condor) for now. Extend as library grows.
    chosen = eligible[0]
    return Choice(chosen, f"{chosen.name}: eligible, VRP={vrp}", confidence)
