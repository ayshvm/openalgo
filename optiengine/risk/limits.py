"""
Risk limits — loads and validates the `risk:` block of config/strategies.yaml.

WHY THIS IS ITS OWN MODULE: the limits in that YAML were written down in Phase 0
but nothing read them, so they were documentation, not control. Every number here
is a hard constraint the Guardian enforces before an order is allowed out. If a
limit cannot be parsed or is self-contradictory, we refuse to start rather than
silently fall back to a default — a risk system that quietly degrades to
permissive is worse than none, because you stop watching.

Phase 0 is stdlib-only, so PyYAML is used when present and a minimal parser for
the flat `risk:` mapping is used when it isn't. The fallback deliberately handles
only `key: value` scalars — if the risk block ever grows nesting, install PyYAML
rather than extending the fallback.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_CONFIG = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                              "config", "strategies.yaml")


class RiskConfigError(ValueError):
    """Raised when limits are missing, unparseable, or internally inconsistent."""


@dataclass(frozen=True)
class RiskLimits:
    """Mirrors the `risk:` block. All percentages are percent, not fractions."""
    max_capital: float             # peak capital the book may command
    deploy_fraction: float         # 0..1 — never margin past this share of capital
    daily_loss_cap_pct: float      # halt + flatten for the day at this loss
    per_structure_max_loss_pct: float
    max_orders_per_second: int     # stay under SEBI's 10 OPS retail threshold
    max_lots_liquidity: int        # per-instrument lot ceiling from ADV analysis

    # Which base the two loss percentages are measured against.
    #   "deployable"    -> max_capital * deploy_fraction (fixed for the day)
    #   "peak_deployed" -> the largest margin actually committed so far today
    # "deployable" is the default because it is a stable number known at 9:15.
    # "peak_deployed" is stricter early in the day: with one small position on,
    # the cap is a small number and ordinary noise can halt you. Choose
    # deliberately — this decides when the kill switch fires.
    loss_cap_basis: str = "deployable"

    @property
    def deployable_capital(self) -> float:
        return self.max_capital * self.deploy_fraction

    def loss_cap_basis_value(self, peak_deployed: float) -> float:
        if self.loss_cap_basis == "peak_deployed":
            return peak_deployed
        return self.deployable_capital

    def daily_loss_cap(self, peak_deployed: float = 0.0) -> float:
        """Absolute rupee loss (a positive number) at which the day halts."""
        return self.loss_cap_basis_value(peak_deployed) * self.daily_loss_cap_pct / 100.0

    def per_structure_max_loss(self, peak_deployed: float = 0.0) -> float:
        """Largest structural max-loss a single new structure may carry."""
        return self.loss_cap_basis_value(peak_deployed) * self.per_structure_max_loss_pct / 100.0

    def validate(self) -> None:
        errs = []
        if self.max_capital <= 0:
            errs.append("max_capital must be > 0")
        if not 0 < self.deploy_fraction <= 1:
            errs.append("deploy_fraction must be in (0, 1]")
        if not 0 < self.daily_loss_cap_pct < 100:
            errs.append("daily_loss_cap_pct must be in (0, 100)")
        if not 0 < self.per_structure_max_loss_pct < 100:
            errs.append("per_structure_max_loss_pct must be in (0, 100)")
        if self.per_structure_max_loss_pct > self.daily_loss_cap_pct:
            errs.append(
                "per_structure_max_loss_pct > daily_loss_cap_pct: a single structure "
                "could blow the daily cap on its own, which makes the daily cap a lie")
        if self.max_orders_per_second <= 0:
            errs.append("max_orders_per_second must be > 0")
        if self.max_orders_per_second >= 10:
            errs.append("max_orders_per_second must stay under SEBI's 10 OPS retail threshold")
        if self.max_lots_liquidity <= 0:
            errs.append("max_lots_liquidity must be > 0")
        if self.loss_cap_basis not in ("deployable", "peak_deployed"):
            errs.append("loss_cap_basis must be 'deployable' or 'peak_deployed'")
        if errs:
            raise RiskConfigError("invalid risk limits: " + "; ".join(errs))


def _parse_risk_block_fallback(text: str) -> dict:
    """Minimal parser for a flat `risk:` mapping. Scalars only, by design."""
    out, in_block = {}, False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        indented = line[:1].isspace()
        if not indented:
            in_block = line.strip() == "risk:"
            continue
        if not in_block:
            continue
        if ":" not in line:
            raise RiskConfigError(f"cannot parse risk limit line: {raw!r}")
        key, _, val = line.strip().partition(":")
        val = val.strip()
        if not val:
            raise RiskConfigError(
                f"risk.{key.strip()} is nested or empty; install PyYAML to load this config")
        out[key.strip()] = val
    return out


def _coerce(raw: dict) -> dict:
    """YAML scalars arrive as str via the fallback and as typed via PyYAML."""
    typed = {}
    for key, val in raw.items():
        if isinstance(val, str):
            try:
                val = float(val) if ("." in val or "e" in val.lower()) else int(val)
            except ValueError:
                pass  # leave strings (loss_cap_basis) alone
        typed[key] = val
    return typed


def load_limits(path: str = DEFAULT_CONFIG) -> RiskLimits:
    """Read, type, validate. Raises RiskConfigError rather than defaulting."""
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        raise RiskConfigError(f"cannot read risk config at {path}: {exc}") from exc

    try:
        import yaml  # type: ignore
    except ImportError:
        raw = _parse_risk_block_fallback(text)
    else:
        doc = yaml.safe_load(text) or {}
        raw = doc.get("risk") or {}

    if not raw:
        raise RiskConfigError(f"no `risk:` block found in {path}")

    typed = _coerce(raw)
    known = set(RiskLimits.__dataclass_fields__)
    unknown = set(typed) - known
    if unknown:
        # A typo'd limit that is silently ignored is a limit that isn't enforced.
        raise RiskConfigError(f"unknown risk keys in {path}: {sorted(unknown)}")
    missing = known - set(typed) - {"loss_cap_basis"}
    if missing:
        raise RiskConfigError(f"missing risk keys in {path}: {sorted(missing)}")

    limits = RiskLimits(**typed)
    limits.validate()
    return limits
