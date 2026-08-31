"""
Guardian — the pre-trade veto and intraday breach detector.

WHY THIS EXISTS: every other module in optiengine answers "what would be a good
trade?". This one answers "is this trade allowed?", and it is the only module
permitted to say yes. Strategy code never talks to a broker directly (see
strategies/base.py), so routing every order through check_entry() gives one
place where capital, per-structure loss, liquidity, order rate and the daily
loss cap are all enforced — and one place to audit after a bad day.

DESIGN STANCE — fail closed. Anything the Guardian cannot evaluate is a denial,
not a pass. Once halted, the Guardian stays halted for the rest of the session
even across a process restart (see killswitch.py); a risk halt that a crash-loop
can clear is not a halt.

The Guardian is deliberately broker-agnostic and pure: it takes numbers in and
returns verdicts. Phase 2's live executor supplies the real margin and MTM; the
backtest can drive the same object with simulated ones. That is what makes it
testable without a broker connection.
"""
from __future__ import annotations

import time as _time
from collections import deque
from dataclasses import dataclass, field

from .limits import RiskLimits


@dataclass(frozen=True)
class ProposedEntry:
    """A structure a strategy wants to open, priced and sized but not yet sent."""
    underlying: str
    lots: int
    n_orders: int                  # legs to be sent (4 for a condor) — each is an order
    margin_required: float         # broker margin for the whole structure
    structural_max_loss: float     # worst case at expiry, after credit received


@dataclass
class OpenStructure:
    """A structure currently on the book."""
    underlying: str
    lots: int
    margin: float
    structural_max_loss: float
    unrealized_pnl: float = 0.0


@dataclass
class Verdict:
    """Result of a pre-trade check. `allowed` is only true when reasons is empty."""
    allowed: bool
    reasons: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.allowed


@dataclass
class Breach:
    """A limit broken on an already-open book. Always actionable, never advisory."""
    kind: str            # "daily_loss_cap" | "capital_deploy"
    detail: str
    flatten: bool        # True => kill switch: close everything, halt for the day


class Guardian:
    """Enforces RiskLimits across a trading session.

    Lifecycle: construct once per session, call check_entry() before every
    structure, record_fill() after orders go out, mark_to_market() on every
    evaluator tick, and honour any Breach returned.
    """

    def __init__(self, limits: RiskLimits, clock=_time.monotonic):
        limits.validate()          # never trust a caller-built limits object
        self.limits = limits
        self._clock = clock
        self._order_times: deque[float] = deque()
        self.open_structures: list[OpenStructure] = []
        self.realized_pnl_today: float = 0.0
        self.peak_deployed: float = 0.0
        self.halted: bool = False
        self.halt_reason: str = ""

    # --- derived book state ------------------------------------------------
    @property
    def deployed_margin(self) -> float:
        return sum(s.margin for s in self.open_structures)

    @property
    def unrealized_pnl(self) -> float:
        return sum(s.unrealized_pnl for s in self.open_structures)

    @property
    def total_pnl_today(self) -> float:
        return self.realized_pnl_today + self.unrealized_pnl

    def lots_on_book(self, underlying: str) -> int:
        return sum(s.lots for s in self.open_structures if s.underlying == underlying)

    def _daily_loss_cap(self) -> float:
        return self.limits.daily_loss_cap(self.peak_deployed)

    def _per_structure_cap(self) -> float:
        return self.limits.per_structure_max_loss(self.peak_deployed)

    # --- order-rate throttle ------------------------------------------------
    def _prune_order_window(self, now: float) -> None:
        while self._order_times and now - self._order_times[0] >= 1.0:
            self._order_times.popleft()

    def orders_in_last_second(self) -> int:
        now = self._clock()
        self._prune_order_window(now)
        return len(self._order_times)

    def record_fill(self, n_orders: int = 1) -> None:
        """Call after orders are actually sent, so the throttle reflects reality."""
        now = self._clock()
        self._prune_order_window(now)
        for _ in range(n_orders):
            self._order_times.append(now)

    # --- pre-trade veto -----------------------------------------------------
    def check_entry(self, entry: ProposedEntry) -> Verdict:
        """Every reason a trade is refused, not just the first one found.

        Returning all of them matters: a structure that is both too large and
        too illiquid should not look like it needs one fix.
        """
        reasons: list[str] = []

        if self.halted:
            reasons.append(f"halted for the session: {self.halt_reason}")

        if entry.lots <= 0 or entry.n_orders <= 0:
            reasons.append("entry must have positive lots and at least one order")
        if entry.margin_required < 0 or entry.structural_max_loss < 0:
            reasons.append("margin and structural_max_loss must be non-negative")

        # Capital: committed margin may never exceed deploy_fraction of capital.
        projected = self.deployed_margin + entry.margin_required
        deployable = self.limits.deployable_capital
        if projected > deployable:
            reasons.append(
                f"capital: margin {projected:,.0f} would exceed deployable "
                f"{deployable:,.0f} ({self.limits.deploy_fraction:.0%} of "
                f"{self.limits.max_capital:,.0f})")

        # Liquidity: per-instrument lot ceiling from the ADV study.
        projected_lots = self.lots_on_book(entry.underlying) + entry.lots
        if projected_lots > self.limits.max_lots_liquidity:
            reasons.append(
                f"liquidity: {projected_lots} lots on {entry.underlying} would exceed "
                f"the {self.limits.max_lots_liquidity}-lot cap")

        # Per-structure loss ceiling. Measured against the basis *after* this
        # entry's margin would land, so peak_deployed mode cannot be gamed by
        # checking against a stale, smaller peak.
        basis_peak = max(self.peak_deployed, projected)
        struct_cap = self.limits.per_structure_max_loss(basis_peak)
        if entry.structural_max_loss > struct_cap:
            reasons.append(
                f"per-structure loss: {entry.structural_max_loss:,.0f} exceeds cap "
                f"{struct_cap:,.0f} ({self.limits.per_structure_max_loss_pct}%)")

        # Daily loss cap: never add risk once the day is already at the cap, and
        # never add a structure whose worst case would breach it outright.
        cap = self.limits.daily_loss_cap(basis_peak)
        if self.total_pnl_today <= -cap:
            reasons.append(
                f"daily loss cap already reached: {self.total_pnl_today:,.0f} "
                f"vs cap {-cap:,.0f}")
        elif self.total_pnl_today - entry.structural_max_loss < -cap:
            reasons.append(
                f"daily loss cap: worst case {self.total_pnl_today - entry.structural_max_loss:,.0f} "
                f"would breach cap {-cap:,.0f}")

        # Order rate: the structure's legs all go out together.
        if self.orders_in_last_second() + entry.n_orders > self.limits.max_orders_per_second:
            reasons.append(
                f"order rate: {self.orders_in_last_second()} + {entry.n_orders} orders "
                f"would exceed {self.limits.max_orders_per_second}/s")

        return Verdict(allowed=not reasons, reasons=reasons)

    # --- book mutation ------------------------------------------------------
    def open_structure(self, entry: ProposedEntry) -> OpenStructure:
        """Record a filled entry. Call ONLY after check_entry() allowed it."""
        verdict = self.check_entry(entry)
        if not verdict.allowed:
            raise RuntimeError("refusing to book a structure the Guardian denied: "
                               + "; ".join(verdict.reasons))
        s = OpenStructure(underlying=entry.underlying, lots=entry.lots,
                          margin=entry.margin_required,
                          structural_max_loss=entry.structural_max_loss)
        self.open_structures.append(s)
        self.record_fill(entry.n_orders)
        self.peak_deployed = max(self.peak_deployed, self.deployed_margin)
        return s

    def close_structure(self, s: OpenStructure, realized_pnl: float,
                        n_orders: int | None = None) -> None:
        """Remove a structure from the book and bank its P&L."""
        self.open_structures.remove(s)
        self.realized_pnl_today += realized_pnl
        self.record_fill(n_orders if n_orders is not None else 1)

    # --- intraday monitoring ------------------------------------------------
    def mark_to_market(self, marks: dict[int, float] | None = None) -> Breach | None:
        """Update unrealized P&L and test the open book against the limits.

        `marks` maps id(structure) -> unrealized P&L. Pass None if the caller
        has already written OpenStructure.unrealized_pnl directly.
        Returns a Breach the caller MUST act on, or None.
        """
        if marks:
            by_id = {id(s): s for s in self.open_structures}
            for key, pnl in marks.items():
                if key not in by_id:
                    # Fail closed: a mark for an unknown structure means the
                    # caller's view of the book and ours have diverged.
                    return self._halt("daily_loss_cap",
                                      f"mark for unknown structure {key}; book desynced")
                by_id[key].unrealized_pnl = pnl

        cap = self._daily_loss_cap()
        if self.total_pnl_today <= -cap:
            return self._halt(
                "daily_loss_cap",
                f"P&L {self.total_pnl_today:,.0f} at or past daily cap {-cap:,.0f} "
                f"(basis {self.limits.loss_cap_basis})")

        deployable = self.limits.deployable_capital
        if self.deployed_margin > deployable:
            # Margin can grow after entry (adverse move, margin re-rate) even
            # though check_entry passed at the time.
            return Breach("capital_deploy",
                          f"margin {self.deployed_margin:,.0f} now exceeds deployable "
                          f"{deployable:,.0f}; reduce size",
                          flatten=False)
        return None

    def _halt(self, kind: str, detail: str) -> Breach:
        self.halted = True
        self.halt_reason = detail
        return Breach(kind, detail, flatten=True)

    def halt(self, reason: str) -> Breach:
        """Manual halt — an operator pulling the plug is a first-class action."""
        return self._halt("manual", reason)
