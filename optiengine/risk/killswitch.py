"""
Kill switch — flatten everything and stay halted for the rest of the session.

WHY A SEPARATE MODULE: the Guardian decides *that* the day is over; this decides
*how* to end it, and the two failure modes are different. A halt that only lives
in memory is undone by a restart — and the situation that trips a kill switch
(a fast adverse move) is exactly the situation in which a process is most likely
to be restarted by a supervisor. So the halt is persisted to disk, keyed by
trading date, and re-read on construction.

FLATTENING IS BEST-EFFORT AND LOUD. If a leg fails to close, we do not retry
forever and we do not pretend it worked: the failure is recorded, the halt stays
in force, and a human is expected to look. Silently half-flattening a hedged
structure leaves a naked short, which is worse than the loss that triggered it.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import date
from typing import Protocol, runtime_checkable

from .guardian import Guardian, OpenStructure

DEFAULT_STATE = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                             ".risk_halt_state.json")


@runtime_checkable
class Flattener(Protocol):
    """Whatever can actually close a structure.

    Phase 2's OpenAlgo executor implements this; the tests use a fake. Keeping
    it a Protocol is what lets the kill switch be exercised with no broker.
    """

    def flatten(self, structure: OpenStructure) -> float:
        """Close every leg. Return realized P&L. Raise on failure to close."""


@dataclass
class FlattenReport:
    halted: bool
    reason: str
    closed: int = 0
    realized_pnl: float = 0.0
    failures: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.failures


class KillSwitch:
    """Persistent session halt + best-effort flatten."""

    def __init__(self, guardian: Guardian, flattener: Flattener,
                 state_path: str = DEFAULT_STATE, today: date | None = None):
        self.guardian = guardian
        self.flattener = flattener
        self.state_path = state_path
        self.today = (today or date.today()).isoformat()
        self._restore()

    # --- persistence --------------------------------------------------------
    def _restore(self) -> None:
        """A halt recorded for *today* survives a restart. Yesterday's does not."""
        try:
            with open(self.state_path, encoding="utf-8") as fh:
                state = json.load(fh)
        except (OSError, ValueError):
            return
        if state.get("date") == self.today and state.get("halted"):
            self.guardian.halted = True
            self.guardian.halt_reason = state.get("reason", "halted earlier today")

    def _persist(self, reason: str) -> None:
        tmp = self.state_path + ".tmp"
        payload = {"date": self.today, "halted": True, "reason": reason}
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            os.replace(tmp, self.state_path)   # atomic: never a half-written halt
        except OSError:
            # Disk trouble must not stop the flatten. The in-memory halt still
            # holds for this process; the restart-safety guarantee is what's lost.
            pass

    # --- the switch ---------------------------------------------------------
    def trip(self, reason: str) -> FlattenReport:
        """Halt the session and close everything on the book."""
        self.guardian.halted = True
        self.guardian.halt_reason = reason
        self._persist(reason)

        report = FlattenReport(halted=True, reason=reason)
        for structure in list(self.guardian.open_structures):
            try:
                pnl = self.flattener.flatten(structure)
            except Exception as exc:
                report.failures.append(
                    f"{structure.underlying} {structure.lots} lots: {exc}")
                continue
            self.guardian.close_structure(structure, pnl)
            report.closed += 1
            report.realized_pnl += pnl
        return report

    def clear(self) -> None:
        """Lift the halt. Manual and deliberate — never called automatically."""
        self.guardian.halted = False
        self.guardian.halt_reason = ""
        try:
            os.remove(self.state_path)
        except OSError:
            pass
