"""Single-side credit verticals — bull put / bear call (defined risk)."""
from __future__ import annotations

from datetime import time
from dataclasses import dataclass

from .base import Action, Adjustment, Leg, OptionType, Regime, Strategy


@dataclass
class BullPutSpread:
    """Sell OTM PE, buy further OTM PE. Bullish / range-up bias."""
    name: str = "Bull Put Spread"
    short_offset: int = 4
    wing_width: int = 2
    target_fraction: float = 0.5
    stop_multiple: float = 1.0
    entry_start: time = time(9, 20)
    entry_end: time = time(10, 30)
    force_exit: time = time(14, 45)

    def eligible(self, regime: Regime) -> bool:
        if regime.is_event_day:
            return False
        return True

    def build_legs(self, regime: Regime) -> list[Leg]:
        if not self.eligible(regime):
            return []
        s = self.short_offset
        h = s + self.wing_width
        return [
            Leg(OptionType.PE, Action.SELL, -s, tag="short_pe"),
            Leg(OptionType.PE, Action.BUY, -h, tag="hedge_pe"),
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


@dataclass
class BearCallSpread:
    """Sell OTM CE, buy further OTM CE. Bearish / range-down bias."""
    name: str = "Bear Call Spread"
    short_offset: int = 4
    wing_width: int = 2
    target_fraction: float = 0.5
    stop_multiple: float = 1.0
    entry_start: time = time(9, 20)
    entry_end: time = time(10, 30)
    force_exit: time = time(14, 45)

    def eligible(self, regime: Regime) -> bool:
        if regime.is_event_day:
            return False
        return True

    def build_legs(self, regime: Regime) -> list[Leg]:
        if not self.eligible(regime):
            return []
        s = self.short_offset
        h = s + self.wing_width
        return [
            Leg(OptionType.CE, Action.SELL, +s, tag="short_ce"),
            Leg(OptionType.CE, Action.BUY, +h, tag="hedge_ce"),
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


def build_bull_put(**kwargs) -> Strategy:
    return BullPutSpread(**kwargs)


def build_bear_call(**kwargs) -> Strategy:
    return BearCallSpread(**kwargs)
