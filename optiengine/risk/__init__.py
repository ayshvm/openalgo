"""Risk layer — the single gate between a strategy's intent and a real order."""
from .guardian import Breach, Guardian, OpenStructure, ProposedEntry, Verdict
from .killswitch import KillSwitch, Flattener
from .limits import RiskLimits, load_limits

__all__ = [
    "Breach", "Guardian", "OpenStructure", "ProposedEntry", "Verdict",
    "KillSwitch", "Flattener", "RiskLimits", "load_limits",
]
