"""Iron Fly — short ATM straddle, long OTM wings (defined risk)."""
from __future__ import annotations

from datetime import time
from dataclasses import dataclass

from .base import Action, Adjustment, Leg, OptionType, Regime, Strategy


@dataclass
class IronFly:
    name: str = "Iron Fly"
    hedge_offset: int = 4
    target_fraction: float = 0.5
    stop_multiple: float = 1.0
    entry_start: time = time(9, 20)
    entry_end: time = time(10, 30)
    force_exit: time = time(14, 45)

    def eligible(self, regime: Regime) -> bool:
        if regime.is_event_day:
            return False
        if regime.trend_score is not None and abs(regime.trend_score) > 0.5:
            return False
        return True

    def build_legs(self, regime: Regime) -> list[Leg]:
        if not self.eligible(regime):
            return []
        w = self.hedge_offset
        return [
            Leg(OptionType.CE, Action.SELL, 0, tag="short_ce"),
            Leg(OptionType.PE, Action.SELL, 0, tag="short_pe"),
            Leg(OptionType.CE, Action.BUY, +w, tag="hedge_ce"),
            Leg(OptionType.PE, Action.BUY, -w, tag="hedge_pe"),
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
    return IronFly(**kwargs)
