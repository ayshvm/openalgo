"""Multi-day holding backtest — does holding longer rescue the economics?

The 1-day round trip pays 8 charged fills (4 legs in, 4 legs out) plus 8 spread
crossings to harvest ONE day of theta. Those costs are per round trip, not per
day held, so holding N days amortizes them over N days of decay. This module
tests whether that actually wins, because the trade-off is real: holding longer
also means more time exposed to a move through the short strikes.

Positions are non-overlapping by default (enter, hold, exit, then look for the
next entry) so results reflect one book being reused, not N books stacked.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ..charges import LegFill, round_trip_charges
from ..strategies.base import Action, Regime, Strategy
from .bhavcopy import (build_chain_for_expiry, build_day_chain, fetch_bhavcopy,
                       fetch_index_opens, trading_days)
from .engine import BacktestConfig, DayResult, SkipDay


@dataclass
class _Loaded:
    rows: list[dict]
    index_opens: dict[str, float]


def _load(day: date, underlying: str, cache_dir: str,
          memo: dict[date, _Loaded | None]) -> _Loaded | None:
    """Load one day's rows, filtered to `underlying` to keep memory sane."""
    if day in memo:
        return memo[day]
    try:
        raw = fetch_bhavcopy(day, cache_dir)
    except Exception:
        raw = None
    if raw is None:
        memo[day] = None
        return None
    rows = [r for r in raw if r.get("TckrSymb") == underlying]
    try:
        idx = fetch_index_opens(day, cache_dir)
    except Exception:
        idx = {}
    memo[day] = _Loaded(rows, idx) if rows else None
    return memo[day]


def run_multiday(strategy: Strategy, cfg: BacktestConfig, hold_days: int = 1,
                 hold_to_expiry: bool = False, progress=print):
    """Enter at open, hold `hold_days` trading days, exit at close of the last one.

    `hold_to_expiry=True` ignores hold_days and exits on the expiry day's close.
    A hold is always truncated at expiry — we never carry past it.
    """
    underlying = cfg.underlyings[0]
    memo: dict[date, _Loaded | None] = {}
    days = [d for d in trading_days(cfg.start, cfg.end)]

    results: list[DayResult] = []
    skips: list[SkipDay] = []
    i = 0
    day_count = 0

    while i < len(days):
        entry_day = days[i]
        loaded = _load(entry_day, underlying, cfg.cache_dir, memo)
        if loaded is None:
            i += 1
            continue
        day_count += 1

        chain = build_day_chain(loaded.rows, underlying, entry_day, loaded.index_opens)
        if chain is None:
            skips.append(SkipDay(entry_day.isoformat(), underlying, "no chain"))
            i += 1
            continue

        expiry = chain.expiry
        exp_date = date.fromisoformat(expiry)
        if exp_date <= entry_day:
            # Expiry day itself: nothing to hold into.
            i += 1
            continue

        regime = Regime(date=entry_day.isoformat(), underlying=underlying,
                        spot=chain.spot, days_to_expiry=(exp_date - entry_day).days)
        legs = strategy.build_legs(regime)
        if not legs:
            skips.append(SkipDay(entry_day.isoformat(), underlying, "strategy declined"))
            i += 1
            continue

        # Freeze absolute strikes at entry.
        resolved = []
        lotsize = None
        bad = False
        for leg in legs:
            strike = chain.strike_for_offset(leg.offset)
            q = chain.quote_at_strike(strike, leg.option_type.value)
            if q is None:
                bad = True
                break
            lotsize = lotsize or q.lotsize
            resolved.append((leg, strike, q))
        if bad:
            skips.append(SkipDay(entry_day.isoformat(), underlying, "missing strike at entry"))
            i += 1
            continue

        qty = cfg.lots * lotsize
        slip = cfg.slippage

        entry_fills = []
        net_credit_per_lot = 0.0
        for leg, strike, q in resolved:
            px = slip.fill_price(q.open, leg.action.value, cfg.lots)
            entry_fills.append(LegFill(leg.action.value, px, qty))
            net_credit_per_lot += (1.0 if leg.action == Action.SELL else -1.0) * px * lotsize

        width_points = 0.0
        shorts = [s for (l, s, _) in resolved if l.action == Action.SELL]
        hedges = [s for (l, s, _) in resolved if l.action == Action.BUY]
        if shorts and hedges:
            width_points = min(abs(h - s) for h in hedges for s in shorts)
        if net_credit_per_lot <= 0 or (width_points and net_credit_per_lot > width_points * lotsize):
            skips.append(SkipDay(entry_day.isoformat(), underlying,
                                 "unreliable credit (stale/illiquid leg price)"))
            i += 1
            continue

        # Walk forward to the exit day: hold_days ahead, truncated at expiry.
        exit_idx, exit_loaded, exit_chain = None, None, None
        j = i
        held = 0
        while j + 1 < len(days):
            j += 1
            nxt = days[j]
            if nxt > exp_date:
                break
            nl = _load(nxt, underlying, cfg.cache_dir, memo)
            if nl is None:
                continue
            nc = build_chain_for_expiry(nl.rows, underlying, expiry, nl.index_opens)
            if nc is None:
                continue
            held += 1
            exit_idx, exit_loaded, exit_chain = j, nl, nc
            if not hold_to_expiry and held >= hold_days:
                break
            if nxt == exp_date:
                break

        if exit_chain is None:
            skips.append(SkipDay(entry_day.isoformat(), underlying, "no exit day available"))
            i += 1
            continue

        exit_fills = []
        missing = False
        for leg, strike, _ in resolved:
            q = exit_chain.quote_at_strike(strike, leg.option_type.value)
            if q is None:
                missing = True
                break
            act = "SELL" if leg.action == Action.BUY else "BUY"
            exit_fills.append(LegFill(act, slip.fill_price(q.close, act, cfg.lots), qty))
        if missing:
            skips.append(SkipDay(entry_day.isoformat(), underlying, "missing strike at exit"))
            i += 1
            continue

        open_val = sum((1 if leg.action == Action.SELL else -1) * f.price * qty
                       for (leg, _, _), f in zip(resolved, entry_fills))
        close_val = sum((1 if leg.action == Action.SELL else -1) * f.price * qty
                        for (leg, _, _), f in zip(resolved, exit_fills))
        gross = open_val - close_val
        charges = round_trip_charges(entry_fills, exit_fills, cfg.rates)["total"]

        results.append(DayResult(
            date=entry_day.isoformat(), underlying=underlying, expiry=expiry,
            spot=chain.spot, lots=cfg.lots,
            net_credit_per_lot=round(net_credit_per_lot, 2),
            gross_pnl=round(gross, 2), charges=round(charges, 2),
            net_pnl=round(gross - charges, 2),
            max_loss_structural=round(width_points * qty - net_credit_per_lot * cfg.lots, 2),
            won=(gross - charges) > 0,
            exit_date=days[exit_idx].isoformat(), held_days=held,
        ))

        # Non-overlapping: next entry is the day after this position closed.
        i = exit_idx + 1

    return results, skips, day_count
