#!/usr/bin/env python
"""
Grid-search many defined-risk option strategies on NIFTY / BANKNIFTY.

Sized for ₹10 Cr capital on Zerodha (exact F&O charges + size-aware slippage).
Preloads bhavcopy + DayChains once; ranks on 1-lot edge; sizes top configs.

Example:
  cd openalgo
  python3.11 -m optiengine.run_sweep --underlyings NIFTY BANKNIFTY --days 365 --html
"""
from __future__ import annotations

import argparse
import json
import os
import time as wall
from dataclasses import dataclass, field
from datetime import date, timedelta
from itertools import product

from .backtest.bhavcopy import (DayChain, build_chain_for_expiry, build_day_chain,
                                fetch_bhavcopy, fetch_index_opens, trading_days)
from .backtest.engine import BacktestConfig, DayResult, SkipDay, simulate_day
from .backtest.fills import SlippageModel
from .backtest.report import summarize
from .charges import ChargeRates, LegFill, round_trip_charges
from .strategies.base import Action, Regime, Strategy
from .strategies.credit_spread import BearCallSpread, BullPutSpread
from .strategies.iron_condor import IronCondor
from .strategies.iron_fly import IronFly
from .strategies.jade_lizard import JadeLizard, ReverseJade
from .strategies.short_strangle import ShortStrangleHedged


CAPITAL = 10_00_00_000
DEPLOY_FRACTION = 0.60
MAX_LOTS_LIQUIDITY = 200
MIN_LOTS = 1
TOP_SIZE_N = 50  # re-sim at ₹10 Cr size for the best 1-lot configs


@dataclass
class SweepConfig:
    family: str
    label: str
    underlying: str
    hold_days: int
    hold_to_expiry: bool
    strategy: object
    short_offset: int | None = None
    hedge_offset: int | None = None
    wing_width: int | None = None


@dataclass
class DayBundle:
    day: date
    rows_by_underlying: dict[str, list[dict]]
    index_opens: dict[str, float]
    chains: dict[str, DayChain | None] = field(default_factory=dict)
    _expiry_cache: dict[tuple[str, str], DayChain | None] = field(default_factory=dict)

    def chain_for_expiry(self, underlying: str, expiry: str) -> DayChain | None:
        key = (underlying, expiry)
        if key not in self._expiry_cache:
            rows = self.rows_by_underlying.get(underlying) or []
            self._expiry_cache[key] = (
                build_chain_for_expiry(rows, underlying, expiry, self.index_opens)
                if rows else None)
        return self._expiry_cache[key]


def _iter_configs(underlying: str, quick: bool = False,
                  extended: bool = False) -> list[SweepConfig]:
    """Defined-risk option-SELLING grid (longs only as hedges)."""
    out: list[SweepConfig] = []
    if quick:
        holds, shorts, wings = [1, 3], [3, 4, 6], [2]
        fly_wings = [3, 5]
        hs_shorts, hs_hedges = [4, 6], [12]
    elif extended:
        holds = [1, 2, 3, 5, 7]
        shorts = [2, 3, 4, 5, 6, 8, 10]
        wings = [1, 2, 3, 4, 5]
        fly_wings = [2, 3, 4, 5, 6, 8, 10]
        hs_shorts = [4, 6, 8, 10, 12]
        hs_hedges = [10, 12, 14, 16, 20]
    else:
        holds = [1, 2, 3, 5]
        shorts = [2, 3, 4, 5, 6, 8]
        wings = [1, 2, 3, 4]
        fly_wings = [2, 3, 4, 5, 6, 8]
        hs_shorts = [4, 6, 8, 10]
        hs_hedges = [10, 12, 14, 16]

    for short, wing, hold in product(shorts, wings, holds):
        hedge = short + wing
        out.append(SweepConfig(
            family="Iron Condor", label=f"IC OTM{short}/{hedge} · {hold}d",
            underlying=underlying, hold_days=hold, hold_to_expiry=False,
            strategy=IronCondor(short_offset=short, hedge_offset=hedge),
            short_offset=short, hedge_offset=hedge, wing_width=wing,
        ))

    for wing, hold in product(fly_wings, holds):
        out.append(SweepConfig(
            family="Iron Fly", label=f"IF wing{wing} · {hold}d",
            underlying=underlying, hold_days=hold, hold_to_expiry=False,
            strategy=IronFly(hedge_offset=wing),
            short_offset=0, hedge_offset=wing, wing_width=wing,
        ))

    for short, hedge, hold in product(hs_shorts, hs_hedges, holds):
        if hedge <= short:
            continue
        out.append(SweepConfig(
            family="Hedged Strangle", label=f"HS OTM{short}/{hedge} · {hold}d",
            underlying=underlying, hold_days=hold, hold_to_expiry=False,
            strategy=ShortStrangleHedged(short_offset=short, hedge_offset=hedge),
            short_offset=short, hedge_offset=hedge, wing_width=hedge - short,
        ))

    for short, wing, hold in product(shorts, wings, holds):
        out.append(SweepConfig(
            family="Bull Put", label=f"BP OTM{short}w{wing} · {hold}d",
            underlying=underlying, hold_days=hold, hold_to_expiry=False,
            strategy=BullPutSpread(short_offset=short, wing_width=wing),
            short_offset=short, wing_width=wing, hedge_offset=short + wing,
        ))
        out.append(SweepConfig(
            family="Bear Call", label=f"BC OTM{short}w{wing} · {hold}d",
            underlying=underlying, hold_days=hold, hold_to_expiry=False,
            strategy=BearCallSpread(short_offset=short, wing_width=wing),
            short_offset=short, wing_width=wing, hedge_offset=short + wing,
        ))

    # Asymmetric ICs (jade / reverse jade) — sell both sides, unequal hedges.
    # Keep this grid tight: full cartesian product explodes config count.
    jade_set = (
        [(4, 4, 6, 2), (4, 4, 4, 2), (6, 4, 6, 2), (4, 6, 6, 2), (3, 3, 4, 2)]
        if not extended else
        [(sp, sc, pw, cw) for sp, sc in product([3, 4, 6], [3, 4, 6])
         for pw, cw in product([4, 6], [1, 2])]
    )
    for sp, sc, pw, cw in jade_set:
        for hold in holds:
            out.append(SweepConfig(
                family="Jade Lizard",
                label=f"JL P{sp}/C{sc} w{pw}/{cw} · {hold}d",
                underlying=underlying, hold_days=hold, hold_to_expiry=False,
                strategy=JadeLizard(short_put=sp, short_call=sc,
                                    put_wing=pw, call_wing=cw),
                short_offset=min(sp, sc),
                hedge_offset=max(sp + pw, sc + cw),
                wing_width=min(pw, cw),
            ))
            out.append(SweepConfig(
                family="Reverse Jade",
                label=f"RJ P{sp}/C{sc} w{cw}/{pw} · {hold}d",
                underlying=underlying, hold_days=hold, hold_to_expiry=False,
                strategy=ReverseJade(short_put=sp, short_call=sc,
                                     put_wing=cw, call_wing=pw),
                short_offset=min(sp, sc),
                hedge_offset=max(sp + cw, sc + pw),
                wing_width=min(pw, cw),
            ))

    if not quick:
        for short, wing in [(3, 2), (4, 2), (5, 2), (6, 2), (4, 4)]:
            hedge = short + wing
            out.append(SweepConfig(
                family="Iron Condor", label=f"IC OTM{short}/{hedge} · to-expiry",
                underlying=underlying, hold_days=1, hold_to_expiry=True,
                strategy=IronCondor(short_offset=short, hedge_offset=hedge),
                short_offset=short, hedge_offset=hedge, wing_width=wing,
            ))
        for wing in [3, 4, 5, 6]:
            out.append(SweepConfig(
                family="Iron Fly", label=f"IF wing{wing} · to-expiry",
                underlying=underlying, hold_days=1, hold_to_expiry=True,
                strategy=IronFly(hedge_offset=wing),
                short_offset=0, hedge_offset=wing, wing_width=wing,
            ))
        for short, wing in [(3, 3), (4, 4), (5, 4), (6, 4)]:
            out.append(SweepConfig(
                family="Bear Call", label=f"BC OTM{short}w{wing} · to-expiry",
                underlying=underlying, hold_days=1, hold_to_expiry=True,
                strategy=BearCallSpread(short_offset=short, wing_width=wing),
                short_offset=short, wing_width=wing, hedge_offset=short + wing,
            ))
            out.append(SweepConfig(
                family="Bull Put", label=f"BP OTM{short}w{wing} · to-expiry",
                underlying=underlying, hold_days=1, hold_to_expiry=True,
                strategy=BullPutSpread(short_offset=short, wing_width=wing),
                short_offset=short, wing_width=wing, hedge_offset=short + wing,
            ))
    return out


def _size_lots(risk_per_lot: float) -> int:
    if risk_per_lot <= 0:
        return MIN_LOTS
    budget = CAPITAL * DEPLOY_FRACTION
    return max(MIN_LOTS, min(int(budget // risk_per_lot), MAX_LOTS_LIQUIDITY))


def preload(underlyings: list[str], start: date, end: date,
            cache_dir: str) -> list[DayBundle]:
    bundles: list[DayBundle] = []
    days = list(trading_days(start, end))
    print(f"Preloading {len(days)} calendar weekdays from cache…", flush=True)
    for i, day in enumerate(days, 1):
        try:
            raw = fetch_bhavcopy(day, cache_dir)
        except Exception:
            continue
        if raw is None:
            continue
        try:
            idx = fetch_index_opens(day, cache_dir)
        except Exception:
            idx = {}
        by_u = {u: [r for r in raw if r.get("TckrSymb") == u] for u in underlyings}
        chains = {
            u: build_day_chain(rows, u, day, idx) if rows else None
            for u, rows in by_u.items()
        }
        bundles.append(DayBundle(day, by_u, idx, chains))
        if i % 50 == 0:
            print(f"  …{i}/{len(days)} ({len(bundles)} trading days)", flush=True)
    print(f"Preloaded {len(bundles)} trading days", flush=True)
    return bundles


def _simulate_same_day(strategy: Strategy, bundles: list[DayBundle],
                       underlying: str, lots: int, slip: SlippageModel,
                       rates: ChargeRates) -> tuple[list[DayResult], list[SkipDay], int]:
    results, skips = [], []
    cfg = BacktestConfig(underlyings=[underlying], start=date.min, end=date.max,
                         lots=lots, slippage=slip, rates=rates)
    for b in bundles:
        chain = b.chains.get(underlying)
        if chain is None:
            skips.append(SkipDay(b.day.isoformat(), underlying, "no chain"))
            continue
        out = simulate_day(strategy, chain, b.day, cfg)
        (results if isinstance(out, DayResult) else skips).append(out)
    return results, skips, len(bundles)


def _simulate_multiday(strategy: Strategy, bundles: list[DayBundle],
                       underlying: str, lots: int, hold_days: int,
                       hold_to_expiry: bool, slip: SlippageModel,
                       rates: ChargeRates) -> tuple[list[DayResult], list[SkipDay], int]:
    results: list[DayResult] = []
    skips: list[SkipDay] = []
    i = 0
    day_count = len(bundles)

    while i < len(bundles):
        b = bundles[i]
        chain = b.chains.get(underlying)
        if chain is None:
            skips.append(SkipDay(b.day.isoformat(), underlying, "no chain"))
            i += 1
            continue

        expiry = chain.expiry
        exp_date = date.fromisoformat(expiry)
        if exp_date <= b.day:
            i += 1
            continue

        regime = Regime(date=b.day.isoformat(), underlying=underlying,
                        spot=chain.spot, days_to_expiry=(exp_date - b.day).days)
        legs = strategy.build_legs(regime)
        if not legs:
            skips.append(SkipDay(b.day.isoformat(), underlying, "strategy declined"))
            i += 1
            continue

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
            skips.append(SkipDay(b.day.isoformat(), underlying, "missing strike at entry"))
            i += 1
            continue

        qty = lots * lotsize
        entry_fills = []
        net_credit_per_lot = 0.0
        for leg, strike, q in resolved:
            px = slip.fills_price(q.open, leg.action.value, lots)
            entry_fills.append(LegFill(leg.action.value, px, qty))
            net_credit_per_lot += (1.0 if leg.action == Action.SELL else -1.0) * px * lotsize

        shorts = [s for (l, s, _) in resolved if l.action == Action.SELL]
        hedges = [s for (l, s, _) in resolved if l.action == Action.BUY]
        width_points = (min(abs(h - s) for h in hedges for s in shorts)
                        if shorts and hedges else 0.0)
        if net_credit_per_lot <= 0 or (width_points and net_credit_per_lot > width_points * lotsize):
            skips.append(SkipDay(b.day.isoformat(), underlying,
                                 "unreliable credit (stale/illiquid leg price)"))
            i += 1
            continue

        exit_idx = exit_chain = None
        held = 0
        j = i
        while j + 1 < len(bundles):
            j += 1
            nb = bundles[j]
            if nb.day > exp_date:
                break
            nc = nb.chain_for_expiry(underlying, expiry)
            if nc is None:
                continue
            held += 1
            exit_idx, exit_chain = j, nc
            if not hold_to_expiry and held >= hold_days:
                break
            if nb.day == exp_date:
                break

        if exit_chain is None:
            skips.append(SkipDay(b.day.isoformat(), underlying, "no exit day available"))
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
            exit_fills.append(LegFill(act, slip.fills_price(q.close, act, lots), qty))
        if missing:
            skips.append(SkipDay(b.day.isoformat(), underlying, "missing strike at exit"))
            i += 1
            continue

        open_val = sum((1 if leg.action == Action.SELL else -1) * f.price * qty
                       for (leg, _, _), f in zip(resolved, entry_fills))
        close_val = sum((1 if leg.action == Action.SELL else -1) * f.price * qty
                        for (leg, _, _), f in zip(resolved, exit_fills))
        gross = open_val - close_val
        charges = round_trip_charges(entry_fills, exit_fills, rates)["total"]
        results.append(DayResult(
            date=b.day.isoformat(), underlying=underlying, expiry=expiry,
            spot=chain.spot, lots=lots,
            net_credit_per_lot=round(net_credit_per_lot, 2),
            gross_pnl=round(gross, 2), charges=round(charges, 2),
            net_pnl=round(gross - charges, 2),
            max_loss_structural=round(width_points * qty - net_credit_per_lot * lots, 2),
            won=(gross - charges) > 0,
            exit_date=bundles[exit_idx].day.isoformat(), held_days=held,
        ))
        i = exit_idx + 1

    return results, skips, day_count


def _sim(cfg_s: SweepConfig, bundles: list[DayBundle], lots: int,
         slip: SlippageModel, rates: ChargeRates):
    if cfg_s.hold_to_expiry or cfg_s.hold_days > 1:
        return _simulate_multiday(
            cfg_s.strategy, bundles, cfg_s.underlying, lots,
            cfg_s.hold_days, cfg_s.hold_to_expiry, slip, rates)
    return _simulate_same_day(
        cfg_s.strategy, bundles, cfg_s.underlying, lots, slip, rates)


def _pack(cfg_s: SweepConfig, results1, skips, day_count, results_sized=None, lots=1):
    if not results1:
        return {
            "family": cfg_s.family, "label": cfg_s.label,
            "underlying": cfg_s.underlying,
            "hold_days": cfg_s.hold_days, "hold_to_expiry": cfg_s.hold_to_expiry,
            "short_offset": cfg_s.short_offset, "hedge_offset": cfg_s.hedge_offset,
            "wing_width": cfg_s.wing_width,
            "lots": 0, "trades": 0, "status": "no_trades",
            "skips": len(skips), "days_scanned": day_count,
        }
    s1 = summarize(results1, cfg_s.underlying) or {}
    risk_per_lot = max(r.max_loss_structural for r in results1)
    sized_lots = _size_lots(risk_per_lot)
    net1 = s1.get("net_total", 0) or 0
    peak1 = s1.get("capital_peak", 0) or 0
    roc1 = (100.0 * net1 / peak1) if peak1 else None

    row = {
        "family": cfg_s.family, "label": cfg_s.label,
        "underlying": cfg_s.underlying,
        "hold_days": cfg_s.hold_days, "hold_to_expiry": cfg_s.hold_to_expiry,
        "short_offset": cfg_s.short_offset, "hedge_offset": cfg_s.hedge_offset,
        "wing_width": cfg_s.wing_width,
        "lots": sized_lots, "risk_per_lot": round(risk_per_lot),
        "trades": s1.get("trades", 0), "days_scanned": day_count, "skips": len(skips),
        "net_1lot": net1, "roc_1lot_pct": roc1,
        "expectancy_1lot": s1.get("expectancy_per_day"),
        "net_per_day_held_1lot": s1.get("net_per_day_held"),
        "win_rate_1lot": s1.get("win_rate"),
        "gross_1lot": s1.get("gross_total"),
        "charges_1lot": s1.get("charges_total"),
        "max_drawdown_1lot": s1.get("max_drawdown"),
        "cvar_5pct_1lot": s1.get("cvar_5pct"),
        "profit_factor_1lot": s1.get("profit_factor"),
        "score": s1.get("net_per_day_held") or 0,
        "status": "ok",
        # sized placeholders — filled for top N
        "net_total": None, "gross_total": None, "charges_total": None,
        "win_rate": s1.get("win_rate"), "profit_factor": s1.get("profit_factor"),
        "max_drawdown": None, "capital_peak": peak1,
        "return_on_capital_pct": roc1, "net_per_day_held": s1.get("net_per_day_held"),
        "cost_drag_pct": s1.get("cost_drag_pct_of_gross"),
    }

    if results_sized:
        s = summarize(results_sized, cfg_s.underlying) or {}
        net = s.get("net_total", 0) or 0
        peak = s.get("capital_peak", 0) or 0
        row.update({
            "lots": lots,
            "net_total": net,
            "gross_total": s.get("gross_total"),
            "charges_total": s.get("charges_total"),
            "win_rate": s.get("win_rate"),
            "profit_factor": s.get("profit_factor"),
            "max_drawdown": s.get("max_drawdown"),
            "cvar_5pct": s.get("cvar_5pct"),
            "worst_day": s.get("worst_day"),
            "capital_peak": peak,
            "return_on_capital_pct": (100.0 * net / peak) if peak else None,
            "net_per_day_held": s.get("net_per_day_held"),
            "cost_drag_pct": s.get("cost_drag_pct_of_gross"),
            "trades": s.get("trades", 0),
        })
    return row


def _print_leaderboard(rows: list[dict], top_n: int = 15):
    ok = [r for r in rows if r.get("status") == "ok" and r.get("trades", 0) >= 15]
    if not ok:
        print("\nNo configs with ≥15 trades.")
        return

    by_score = sorted(ok, key=lambda r: r.get("score") or -1e18, reverse=True)
    by_net1 = sorted(ok, key=lambda r: r.get("net_1lot") or -1e18, reverse=True)
    sized = [r for r in ok if r.get("net_total") is not None]
    by_net = sorted(sized, key=lambda r: r.get("net_total") or -1e18, reverse=True)

    def _fmt(r, i):
        wr = (r.get("win_rate_1lot") or 0) * 100
        roc1 = r.get("roc_1lot_pct")
        roc_s = f"{roc1:+.1f}%" if roc1 is not None else "n/a"
        n1 = r.get("net_1lot") or 0
        ns = r.get("net_total")
        ns_s = f"₹{ns:>12,}" if ns is not None else "        (n/a)"
        return (f"  {i:>2}. {r['underlying']:<10} {r['label']:<28} "
                f"1lot=₹{n1:>10,} ({roc_s:>7})  "
                f"@{r['lots']}lots={ns_s}  "
                f"WR={wr:4.0f}%  ₹/day1={r.get('net_per_day_held_1lot') or 0:>8,}")

    print(f"\n{'='*110}")
    print(f"  TOP {top_n} BY 1-LOT ₹/DAY HELD  (structure edge; Zerodha costs ON)")
    print(f"{'='*110}")
    for i, r in enumerate(by_score[:top_n], 1):
        print(_fmt(r, i))

    print(f"\n{'='*110}")
    print(f"  TOP {top_n} BY 1-LOT NET P&L")
    print(f"{'='*110}")
    for i, r in enumerate(by_net1[:top_n], 1):
        print(_fmt(r, i))

    if by_net:
        print(f"\n{'='*110}")
        print(f"  TOP {min(top_n, len(by_net))} BY SIZED NET P&L (₹10 Cr book)")
        print(f"{'='*110}")
        for i, r in enumerate(by_net[:top_n], 1):
            print(_fmt(r, i))

    print(f"\n{'='*110}")
    print("  BEST PER UNDERLYING × FAMILY (by 1-lot net)")
    print(f"{'='*110}")
    seen = set()
    for r in by_net1:
        key = (r["underlying"], r["family"])
        if key in seen:
            continue
        seen.add(key)
        print(_fmt(r, len(seen)))

    profitable = [r for r in ok if (r.get("net_1lot") or 0) > 0]
    profitable_sized = [r for r in sized if (r.get("net_total") or 0) > 0]
    print(f"\n  1-lot net-positive: {len(profitable)}/{len(ok)}  |  "
          f"sized net-positive: {len(profitable_sized)}/{len(sized)}  "
          f"(of {len(rows)} total tried)")


def _write_html(rows: list[dict], path: str, capital: int):
    ok = [r for r in rows if r.get("status") == "ok"]
    ok_sorted = sorted(ok, key=lambda r: r.get("net_1lot") or -1e18, reverse=True)
    profitable = [r for r in ok_sorted if (r.get("net_1lot") or 0) > 0]

    def row_html(r, i):
        net1 = r.get("net_1lot") or 0
        net = r.get("net_total")
        cls = "pos" if net1 > 0 else "neg"
        wr = (r.get("win_rate_1lot") or 0) * 100
        roc1 = r.get("roc_1lot_pct")
        roc_s = f"{roc1:+.1f}%" if roc1 is not None else "—"
        net_s = f"₹{net:,}" if net is not None else "—"
        return (
            f"<tr class='{cls}'><td>{i}</td><td>{r['underlying']}</td>"
            f"<td>{r['family']}</td><td>{r['label']}</td><td>{r['lots']}</td>"
            f"<td>{r.get('trades',0)}</td>"
            f"<td>₹{net1:,}</td><td>{roc_s}</td>"
            f"<td>{net_s}</td><td>{wr:.0f}%</td>"
            f"<td>{r.get('profit_factor_1lot') if r.get('profit_factor_1lot') is not None else '—'}</td>"
            f"<td>₹{r.get('net_per_day_held_1lot') or 0:,}</td>"
            f"<td>₹{r.get('max_drawdown') or r.get('max_drawdown_1lot') or 0:,}</td></tr>"
        )

    body_rows = "\n".join(row_html(r, i) for i, r in enumerate(ok_sorted[:100], 1))
    best = ok_sorted[0] if ok_sorted else None
    verdict = (
        f"<p class='verdict'>Best 1-lot edge: <b>{best['underlying']} · {best['label']}</b> "
        f"→ ₹{best.get('net_1lot') or 0:,}/lot "
        f"(ROC {best.get('roc_1lot_pct') or 0:+.1f}%, "
        f"WR {(best.get('win_rate_1lot') or 0)*100:.0f}%). "
        f"Sized ({best['lots']} lots): "
        f"{'₹' + format(best.get('net_total'), ',') if best.get('net_total') is not None else 'not re-simmed'}. "
        f"{len(profitable)}/{len(ok_sorted)} configs net-positive at 1 lot after Zerodha costs.</p>"
        if best else "<p>No tradeable configs.</p>"
    )

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>OptiEngine Strategy Sweep</title>
<style>
body{{font-family:ui-sans-serif,system-ui,sans-serif;margin:24px;background:#0f1419;color:#e7ecf3}}
h1{{font-size:1.4rem;margin:0 0 8px}}
.meta{{color:#9aa7b8;margin-bottom:20px}}
.verdict{{background:#1a2332;padding:14px 16px;border-radius:8px;border-left:3px solid #3d8bfd}}
table{{border-collapse:collapse;width:100%;font-size:13px;margin-top:16px}}
th,td{{padding:7px 8px;border-bottom:1px solid #243044;text-align:right}}
th{{color:#9aa7b8;font-weight:600;position:sticky;top:0;background:#0f1419}}
td:nth-child(2),td:nth-child(3),td:nth-child(4),th:nth-child(2),th:nth-child(3),th:nth-child(4)
{{text-align:left}}
tr.pos td:nth-child(7){{color:#3dd68c}}
tr.neg td:nth-child(7){{color:#f07178}}
.note{{color:#9aa7b8;font-size:12px;margin-top:18px;max-width:780px;line-height:1.5}}
</style></head><body>
<h1>Strategy sweep — NIFTY / BANKNIFTY · ₹{capital/1e7:.0f} Cr Zerodha</h1>
<p class="meta">{len(rows)} configs · {len(ok_sorted)} with trades ·
{len(profitable)} net-positive at 1 lot · EOD bhavcopy open→close · exact F&amp;O charges + slippage</p>
{verdict}
<table>
<thead><tr>
<th>#</th><th>Underlying</th><th>Family</th><th>Config</th><th>Lots</th>
<th>Trades</th><th>Net 1-lot</th><th>ROC 1-lot</th><th>Net sized</th><th>Win%</th><th>PF</th>
<th>₹/day 1-lot</th><th>Max DD</th>
</tr></thead>
<tbody>
{body_rows}
</tbody></table>
<p class="note">
Honest limits: bhavcopy is EOD — no intraday stop/target simulation. BANKNIFTY is
monthly-only expiry, so “daily” setups often hold options weeks from expiry.
Capital peak = structural max loss (width − credit); Zerodha SPAN+exposure margin
is typically higher — verify before live sizing. Naked structures excluded.
If 1-lot expectancy is negative, optimal size for a ₹10 Cr book is zero — scaling
only magnifies the loss via impact.
</p>
</body></html>"""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        f.write(html)
    return path


def main():
    ap = argparse.ArgumentParser(description="OptiEngine strategy grid search (₹10 Cr Zerodha)")
    ap.add_argument("--underlyings", nargs="+", default=["NIFTY", "BANKNIFTY"])
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--spread-pct", type=float, default=0.01)
    ap.add_argument("--cache-dir", default=os.path.join(os.path.dirname(__file__), ".bhavcopy_cache"))
    ap.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "sweep_out.json"))
    ap.add_argument("--html", nargs="?", const="auto", default=None)
    ap.add_argument("--open", action="store_true", dest="open_html")
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--extended", action="store_true",
                    help="wider strike/hold grid + more jade variants")
    args = ap.parse_args()

    start = date.today() - timedelta(days=args.days)
    end = date.today() - timedelta(days=1)

    configs: list[SweepConfig] = []
    for u in args.underlyings:
        configs.extend(_iter_configs(u, quick=args.quick, extended=args.extended))

    print(f"Sweeping {len(configs)} configs on {args.underlyings} "
          f"({args.days}d lookback, capital ₹{CAPITAL/1e7:.0f} Cr, "
          f"deploy ≤{DEPLOY_FRACTION:.0%}, Zerodha charges ON)", flush=True)

    bundles = preload(args.underlyings, start, end, args.cache_dir)
    if not bundles:
        print("No bhavcopy days loaded — check cache / network.")
        return

    slip = SlippageModel(spread_pct=args.spread_pct)
    rates = ChargeRates()
    t0 = wall.time()
    rows = []
    cfg_by_key = {}

    for i, c in enumerate(configs, 1):
        results1, skips, day_count = _sim(c, bundles, 1, slip, rates)
        row = _pack(c, results1, skips, day_count)
        rows.append(row)
        cfg_by_key[(c.underlying, c.label)] = c
        n1 = row.get("net_1lot")
        n1s = f"₹{n1:,}" if isinstance(n1, (int, float)) else row.get("status")
        if i % 25 == 0 or i == len(configs):
            print(f"  [{i:>3}/{len(configs)}] last={c.underlying} {c.label:<28} {n1s}",
                  flush=True)

    # Size the least-bad / best 1-lot configs at ₹10 Cr liquidity cap.
    candidates = sorted(
        [r for r in rows if r.get("status") == "ok"],
        key=lambda r: r.get("net_1lot") or -1e18, reverse=True,
    )[:TOP_SIZE_N]
    print(f"\nRe-simulating top {len(candidates)} at ₹10 Cr-sized lots…", flush=True)
    for r in candidates:
        c = cfg_by_key[(r["underlying"], r["label"])]
        lots = r["lots"] or 1
        results_s, skips, day_count = _sim(c, bundles, lots, slip, rates)
        if results_s:
            s = summarize(results_s, c.underlying) or {}
            net = s.get("net_total", 0) or 0
            peak = s.get("capital_peak", 0) or 0
            r.update({
                "lots": lots,
                "net_total": net,
                "gross_total": s.get("gross_total"),
                "charges_total": s.get("charges_total"),
                "win_rate": s.get("win_rate"),
                "profit_factor": s.get("profit_factor"),
                "max_drawdown": s.get("max_drawdown"),
                "cvar_5pct": s.get("cvar_5pct"),
                "worst_day": s.get("worst_day"),
                "capital_peak": peak,
                "return_on_capital_pct": (100.0 * net / peak) if peak else None,
                "net_per_day_held": s.get("net_per_day_held"),
                "cost_drag_pct": s.get("cost_drag_pct_of_gross"),
                "trades": s.get("trades", r.get("trades")),
            })
            print(f"  sized {c.underlying} {c.label:<28} lots={lots} net=₹{net:,}",
                  flush=True)

    elapsed = wall.time() - t0
    print(f"\nDone in {elapsed/60:.1f} min ({len(configs)} configs)", flush=True)

    payload = {
        "meta": {
            "capital": CAPITAL, "deploy_fraction": DEPLOY_FRACTION,
            "underlyings": args.underlyings, "days": args.days,
            "spread_pct": args.spread_pct,
            "start": start.isoformat(), "end": end.isoformat(),
            "n_configs": len(configs), "trading_days": len(bundles),
            "elapsed_sec": round(elapsed, 1), "broker": "zerodha",
            "top_sized": TOP_SIZE_N,
        },
        "results": rows,
    }
    with open(args.out, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"Full results -> {args.out}", flush=True)

    _print_leaderboard(rows, top_n=args.top)

    if args.html is not None:
        html_path = (os.path.join(os.path.dirname(__file__), "reports", "sweep.html")
                     if args.html == "auto" else args.html)
        _write_html(rows, html_path, CAPITAL)
        print(f"HTML report  -> {html_path}", flush=True)
        if args.open_html:
            import webbrowser
            webbrowser.open(f"file://{os.path.abspath(html_path)}")


if __name__ == "__main__":
    main()
