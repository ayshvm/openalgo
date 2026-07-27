"""Short strangle with far wings — defined-risk (wide iron condor).

Naked short strangles are excluded: unlimited risk + SPAN margin is not
modeled honestly from bhavcopy. Wings define structural max loss = capital.
"""
from __future__ import annotations

from datetime import time
from dataclasses import dataclass

from .base import Action, Adjustment, Leg, OptionType, Regime, Strategy


@dataclass
class ShortStrangleHedged:
    name: str = "Hedged Short Strangle"
    short_offset: int = 6
    hedge_offset: int = 12
    target_fraction: float = 0.5
    stop_multiple: float = 1.0
    entry_start: time = time(9, 20)
    entry_end: time = time(10, 30)
    force_exit: time = time(14, 45)

    def eligible(self, regime: Regime) -> bool:
        if regime.is_event_day:
            return False
        if regime.trend_score is not None and abs(regime.trend_score) > 0.7:
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
        return None


def build(**kwargs) -> Strategy:
    return ShortStrangleHedged(**kwargs)
