"""
Regime feature computation. Phase-0 version derives what it can from bhavcopy
(expected move via ATM straddle, realized move via recent spot ranges, rough
trend). Live version will fill iv_rank/trend from the OpenAlgo feed. The Regime
object is identical either way, so the selector code doesn't care which fed it.
"""
from __future__ import annotations

from datetime import date

from ..strategies.base import Regime
from ..backtest.bhavcopy import DayChain


def expected_move_pct(chain: DayChain) -> float | None:
    ce = chain.quote_at_offset(0, "CE")
    pe = chain.quote_at_offset(0, "PE")
    if ce and pe and chain.spot > 0:
        return (ce.open + pe.open) / chain.spot
    return None


def realized_move_pct(recent_spots: list[float]) -> float | None:
    """Mean absolute daily return over a lookback (feed it recent closes)."""
    if len(recent_spots) < 3:
        return None
    rets = [abs(recent_spots[i] / recent_spots[i - 1] - 1) for i in range(1, len(recent_spots))]
    return sum(rets) / len(rets)


def build_regime(chain: DayChain, trade_day: date, recent_spots: list[float] | None = None,
                 event_days: set[str] | None = None) -> Regime:
    em = expected_move_pct(chain)
    rm = realized_move_pct(recent_spots or [])
    y, m, d = chain.expiry[:4], chain.expiry[5:7], chain.expiry[8:10]
    try:
        dte = max((date(int(y), int(m), int(d)) - trade_day).days, 0)
    except Exception:
        dte = 0
    is_event = bool(event_days and trade_day.isoformat() in event_days)
    return Regime(
        date=trade_day.isoformat(), underlying=chain.underlying, spot=chain.spot,
        days_to_expiry=dte, expected_move_pct=em, realized_move_pct=rm,
        is_event_day=is_event,
        extras={"vrp": (em - rm) if (em is not None and rm is not None) else None},
    )
