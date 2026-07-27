"""Jade Lizard / reverse lizard — short strangle with one wing (defined on one side).

Jade Lizard (bullish bias): sell OTM PE + sell OTM CE + buy further OTM CE.
  Put side is naked structurally; we still require the call wing so max loss on
  the upside is capped. For the naked put side we add a far put hedge so the
  structure stays defined-risk for capital sizing (sell+hedge only).

This module implements the *defined-risk* jade: short strangle + long call wing
+ long put wing farther than the short put (asymmetric wings).
"""
from __future__ import annotations

from datetime import time
from dataclasses import dataclass

from .base import Action, Adjustment, Leg, OptionType, Regime, Strategy


@dataclass
class JadeLizard:
    """Asymmetric iron condor: tighter call wing, wider put wing (or vice versa)."""
    name: str = "Jade Lizard"
    short_put: int = 4
    short_call: int = 4
    put_wing: int = 6          # put hedge = short_put + put_wing
    call_wing: int = 2         # call hedge = short_call + call_wing
    target_fraction: float = 0.5
    stop_multiple: float = 1.0
    entry_start: time = time(9, 20)
    entry_end: time = time(10, 30)
    force_exit: time = time(14, 45)

    def eligible(self, regime: Regime) -> bool:
        return not regime.is_event_day

    def build_legs(self, regime: Regime) -> list[Leg]:
        if not self.eligible(regime):
            return []
        return [
            Leg(OptionType.PE, Action.SELL, -self.short_put, tag="short_pe"),
            Leg(OptionType.CE, Action.SELL, +self.short_call, tag="short_ce"),
            Leg(OptionType.PE, Action.BUY, -(self.short_put + self.put_wing), tag="hedge_pe"),
            Leg(OptionType.CE, Action.BUY, +(self.short_call + self.call_wing), tag="hedge_ce"),
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
class ReverseJade:
    """Tighter put wing, wider call wing — bearish-leaning asymmetric IC."""
    name: str = "Reverse Jade"
    short_put: int = 4
    short_call: int = 4
    put_wing: int = 2
    call_wing: int = 6
    target_fraction: float = 0.5
    stop_multiple: float = 1.0
    entry_start: time = time(9, 20)
    entry_end: time = time(10, 30)
    force_exit: time = time(14, 45)

    def eligible(self, regime: Regime) -> bool:
        return not regime.is_event_day

    def build_legs(self, regime: Regime) -> list[Leg]:
        if not self.eligible(regime):
            return []
        return [
            Leg(OptionType.PE, Action.SELL, -self.short_put, tag="short_pe"),
            Leg(OptionType.CE, Action.SELL, +self.short_call, tag="short_ce"),
            Leg(OptionType.PE, Action.BUY, -(self.short_put + self.put_wing), tag="hedge_pe"),
            Leg(OptionType.CE, Action.BUY, +(self.short_call + self.call_wing), tag="hedge_ce"),
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


def build_jade(**kwargs) -> Strategy:
    return JadeLizard(**kwargs)


def build_reverse(**kwargs) -> Strategy:
    return ReverseJade(**kwargs)
