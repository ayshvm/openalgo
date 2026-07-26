"""
Strategy interface — the single contract every strategy implements so the
selector, backtester, and live executor can treat them interchangeably.

Design goal: the SAME Strategy object is used to (a) backtest against historical
bhavcopy chains and (b) trade live via OpenAlgo. The strategy only ever describes
*what legs to hold and when to enter/adjust/exit* — it never talks to a broker or
a data source directly. That keeps strategy logic pure and testable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time
from enum import Enum
from typing import Protocol, runtime_checkable


class OptionType(str, Enum):
    CE = "CE"
    PE = "PE"


class Action(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass(frozen=True)
class Leg:
    """One option leg, expressed in strategy-neutral terms (offset from ATM).

    The concrete strike/symbol is resolved later by the data layer (backtest) or
    OpenAlgo optionchain (live), so the same Leg spec works in both worlds.
    """
    option_type: OptionType
    action: Action
    offset: int          # strikes from ATM: +N above spot, -N below. Sign is by convention per side.
    lots: int = 1
    tag: str = ""        # e.g. "short_ce", "hedge_pe" — for adjustment/reporting


@dataclass(frozen=True)
class Regime:
    """Market state the selector uses to choose a strategy. Computed from data,
    identical fields whether it came from historical bhavcopy or a live feed."""
    date: str
    underlying: str
    spot: float
    days_to_expiry: int
    iv_rank: float | None = None          # 0..100, None if unknown (bhavcopy has no IV directly)
    expected_move_pct: float | None = None  # implied (ATM straddle / spot)
    realized_move_pct: float | None = None  # recent realized daily range
    trend_score: float | None = None        # -1..1 directional lean
    is_event_day: bool = False              # RBI/budget/results cluster/expiry-gamma
    extras: dict = field(default_factory=dict)


class ExitReason(str, Enum):
    PROFIT_TARGET = "profit_target"
    STOP_LOSS = "stop_loss"
    TIME_EXIT = "time_exit"
    EVENT_STANDDOWN = "event_standdown"
    GUARDIAN = "guardian_forced"


@dataclass
class Adjustment:
    """A requested change to an open position (roll a side, add a hedge, reduce)."""
    reason: str
    close_tags: list[str] = field(default_factory=list)   # legs to close
    open_legs: list[Leg] = field(default_factory=list)    # legs to open


@runtime_checkable
class Strategy(Protocol):
    name: str

    def eligible(self, regime: Regime) -> bool:
        """Can this strategy be considered at all in this regime? (cheap gate)"""

    def build_legs(self, regime: Regime) -> list[Leg]:
        """Return the leg spec to enter, given the regime. Sizing (lots) may be a
        placeholder of 1 here — the executor/backtester scales it to capital via
        the broker/margin model. Return [] to decline entry."""

    def entry_window(self) -> tuple[time, time]:
        """IST (start, end) during which entry is allowed."""

    def profit_target_fraction(self) -> float:
        """Fraction of received credit at which to book profit (e.g. 0.5)."""

    def stop_loss_multiple(self) -> float:
        """Multiple of received credit at which to stop out as a loss (e.g. 1.0)."""

    def force_exit_time(self) -> time:
        """Latest IST time to hold; flatten at/after this."""

    def should_adjust(self, position, market) -> Adjustment | None:
        """Optional intraday adjustment. Return None for no change."""
