"""
Iron Condor — refactor of your strategies/examples/iron_condor_daily.py into the
Strategy interface. Same economics: sell OTM4 CE+PE (theta engine), buy OTM6
CE+PE (defined-risk insurance), book 50% of credit, stop at 100% of credit,
force-exit at 14:45 IST. Enter 09:20–10:30.

The live-broker plumbing (margin sizing, order placement, position polling) that
lived in the original script now lives in live/executor.py; this file is pure
strategy logic so the SAME object backtests and trades.
"""
from __future__ import annotations

from datetime import time
from dataclasses import dataclass

from .base import Action, Adjustment, Leg, OptionType, Regime, Strategy


@dataclass
class IronCondor:
    name: str = "Intraday Iron Condor"
    short_offset: int = 4          # OTM4 short legs (the premium/theta engine)
    hedge_offset: int = 6          # OTM6 long legs (the insurance / defined-risk cap)
    target_fraction: float = 0.5
    stop_multiple: float = 1.0
    entry_start: time = time(9, 20)
    entry_end: time = time(10, 30)
    force_exit: time = time(14, 45)
    # adjustment: if net delta on one side gets tested, roll the untested side in.
    enable_adjustment: bool = False

    def eligible(self, regime: Regime) -> bool:
        # Stand down on event days (expiry-gamma, RBI/budget/results cluster).
        if regime.is_event_day:
            return False
        # Condor wants range-bound / non-trending. If we have a trend score, avoid
        # strong trends. If we don't (bhavcopy-only), stay eligible.
        if regime.trend_score is not None and abs(regime.trend_score) > 0.6:
            return False
        return True

    def build_legs(self, regime: Regime) -> list[Leg]:
        if not self.eligible(regime):
            return []
        return [
            Leg(OptionType.CE, Action.SELL, +self.short_offset, tag="short_ce"),
            Leg(OptionType.PE, Action.SELL, -self.short_offset, tag="short_pe"),
            Leg(OptionType.CE, Action.BUY, +self.hedge_offset, tag="hedge_ce"),
            Leg(OptionType.PE, Action.BUY, -self.hedge_offset, tag="hedge_pe"),
        ]

    def entry_window(self) -> tuple[time, time]:
        return (self.entry_start, self.entry_end)

    def profit_target_fraction(self) -> float:
        return self.target_fraction

    def stop_loss_multiple(self) -> float:
        return self.stop_multiple

    def force_exit_time(self) -> time:
        return self.force_exit

    def should_adjust(self, position, market) -> Adjustment | None:
        # Placeholder for Phase 3 delta-management. Off by default so backtest
        # matches your current live behavior (enter-and-hold-with-exits).
        return None


# Register-style factory so config/strategies.yaml can name it.
def build(**kwargs) -> Strategy:
    return IronCondor(**kwargs)
