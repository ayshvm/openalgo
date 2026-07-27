"""
Backtest engine — runs any Strategy over historical bhavcopy chains.

Honest about data limits (same stance as your original backtester):
  * Bhavcopy is EOD (one O/H/L/C per contract), so the trustworthy simulation is
    ENTRY-AT-OPEN → EXIT-AT-CLOSE. We do NOT pretend to trigger intraday
    profit-target/stop-loss from four independent daily H/L ranges.
  * We DO now add what your version lacked: realistic fills (spread+impact) and
    exact charges (STT/brokerage/txn/GST/stamp/SEBI) on all 8 round-trip fills.
  * Bad-data days (credit <= 0, credit > width, missing strike) are skipped and
    counted — never traded on a number the market never offered.

Result per day includes gross AND net (after costs) so you can see how much of
the edge the costs consume — the number that actually matters at ₹10 Cr.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from ..charges import ChargeRates, LegFill, round_trip_charges
from ..strategies.base import Action, Regime, Strategy
from .bhavcopy import (DayChain, build_day_chain, fetch_bhavcopy,
                       fetch_index_opens, trading_days)
from .fills import SlippageModel


@dataclass
class DayResult:
    date: str
    underlying: str
    expiry: str
    spot: float
    lots: int
    net_credit_per_lot: float
    gross_pnl: float
    charges: float
    net_pnl: float
    max_loss_structural: float
    won: bool
    exit_date: str = ""      # multi-day holds: when the position was closed
    held_days: int = 1       # trading days held (1 = same-day round trip)


@dataclass
class SkipDay:
    date: str
    underlying: str
    reason: str


@dataclass
class BacktestConfig:
    underlyings: list[str]
    start: date
    end: date
    lots: int = 1
    cache_dir: str = ".bhavcopy_cache"
    slippage: SlippageModel = field(default_factory=SlippageModel)
    rates: ChargeRates = field(default_factory=ChargeRates)


def _regime_from_chain(chain: DayChain, trade_day: date) -> Regime:
    # Minimal regime from bhavcopy: expected move via ATM straddle / spot.
    ce = chain.quote_at_offset(0, "CE")
    pe = chain.quote_at_offset(0, "PE")
    exp_move = None
    if ce and pe and chain.spot > 0:
        exp_move = (ce.open + pe.open) / chain.spot
    # days to expiry (rough, calendar)
    y, m, d = chain.expiry[:4], chain.expiry[5:7], chain.expiry[8:10]
    try:
        dte = (date(int(y), int(m), int(d)) - trade_day).days
    except Exception:
        dte = 0
    return Regime(date=trade_day.isoformat(), underlying=chain.underlying,
                  spot=chain.spot, days_to_expiry=max(dte, 0),
                  expected_move_pct=exp_move)


def simulate_day(strategy: Strategy, chain: DayChain, trade_day: date,
                 cfg: BacktestConfig) -> DayResult | SkipDay:
    regime = _regime_from_chain(chain, trade_day)
    legs = strategy.build_legs(regime)
    if not legs:
        return SkipDay(trade_day.isoformat(), chain.underlying, "strategy declined (regime)")

    # Resolve each leg's quote; bail on any missing strike.
    resolved = []
    lotsize = None
    for leg in legs:
        q = chain.quote_at_offset(leg.offset, leg.option_type.value)
        if q is None:
            return SkipDay(trade_day.isoformat(), chain.underlying,
                           f"missing strike offset {leg.offset} {leg.option_type.value}")
        lotsize = lotsize or q.lotsize
        resolved.append((leg, q))

    qty = cfg.lots * lotsize
    slip = cfg.slippage

    # Entry fills (open) and exit fills (close), always adverse.
    net_credit_per_lot = 0.0
    entry_fills, exit_fills = [], []
    short_strikes, hedge_strikes = [], []
    for leg, q in resolved:
        entry_px = slip.fill_price(q.open, leg.action.value, cfg.lots)
        exit_action = "SELL" if leg.action == Action.BUY else "BUY"
        exit_px = slip.fill_price(q.close, exit_action, cfg.lots)
        entry_fills.append(LegFill(leg.action.value, entry_px, qty))
        exit_fills.append(LegFill(exit_action, exit_px, qty))
        sign = 1.0 if leg.action == Action.SELL else -1.0
        net_credit_per_lot += sign * entry_px * lotsize
        (short_strikes if leg.action == Action.SELL else hedge_strikes).append(
            chain.strike_for_offset(leg.offset))

    # Sanity: a credit structure's credit can't be <= 0 or exceed wing width.
    # Use min short↔hedge distance (works for condors, flies, and single verticals).
    width_points = 0.0
    if short_strikes and hedge_strikes:
        width_points = min(abs(h - s) for h in hedge_strikes for s in short_strikes)
    if net_credit_per_lot <= 0 or (width_points and net_credit_per_lot > width_points * lotsize):
        return SkipDay(trade_day.isoformat(), chain.underlying,
                       "unreliable credit (stale/illiquid leg price)")

    # Gross P&L: value change of the whole structure open->close.
    open_val = sum((1 if leg.action == Action.SELL else -1) *
                   ef.price * qty for (leg, _), ef in zip(resolved, entry_fills))
    close_val = sum((1 if leg.action == Action.SELL else -1) *
                    xf.price * qty for (leg, _), xf in zip(resolved, exit_fills))
    gross_pnl = open_val - close_val  # seller profits if structure value falls

    charges = round_trip_charges(entry_fills, exit_fills, cfg.rates)["total"]
    net_pnl = gross_pnl - charges
    max_loss_structural = width_points * qty - net_credit_per_lot * cfg.lots

    return DayResult(
        date=trade_day.isoformat(), underlying=chain.underlying, expiry=chain.expiry,
        spot=chain.spot, lots=cfg.lots,
        net_credit_per_lot=round(net_credit_per_lot, 2),
        gross_pnl=round(gross_pnl, 2), charges=round(charges, 2),
        net_pnl=round(net_pnl, 2), max_loss_structural=round(max_loss_structural, 2),
        won=net_pnl > 0,
    )


def run(strategy: Strategy, cfg: BacktestConfig, progress=print):
    results: list[DayResult] = []
    skips: list[SkipDay] = []
    day_count = 0
    for day in trading_days(cfg.start, cfg.end):
        try:
            rows = fetch_bhavcopy(day, cfg.cache_dir)
        except Exception as e:
            progress(f"  {day}: fetch failed ({e})")
            continue
        if rows is None:
            continue
        day_count += 1
        try:
            index_opens = fetch_index_opens(day, cfg.cache_dir)
        except Exception as e:
            progress(f"  {day}: index open fetch failed ({e})")
            index_opens = {}
        for underlying in cfg.underlyings:
            chain = build_day_chain(rows, underlying, day, index_opens)
            if chain is None:
                skips.append(SkipDay(day.isoformat(), underlying, "no chain"))
                continue
            out = simulate_day(strategy, chain, day, cfg)
            (results if isinstance(out, DayResult) else skips).append(out)
    return results, skips, day_count
