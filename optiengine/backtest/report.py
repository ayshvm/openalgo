"""
Backtest metrics + report. Reports GROSS and NET side by side so the cost drag
is explicit, plus the risk metrics that matter for a seller: max drawdown,
worst day, and tail (CVaR) — the days that actually blow up sellers.
"""
from __future__ import annotations

import statistics
from dataclasses import asdict

from .engine import DayResult, SkipDay


def _drawdown(equity: list[float]) -> float:
    peak = equity[0] if equity else 0.0
    max_dd = 0.0
    for v in equity:
        peak = max(peak, v)
        max_dd = min(max_dd, v - peak)
    return max_dd


def summarize(results: list[DayResult], underlying: str) -> dict:
    rows = [r for r in results if r.underlying == underlying]
    if not rows:
        return {}
    net = [r.net_pnl for r in rows]
    gross = [r.gross_pnl for r in rows]
    charges = [r.charges for r in rows]
    wins = [p for p in net if p > 0]
    losses = [p for p in net if p <= 0]

    equity, run = [], 0.0
    for p in net:
        run += p
        equity.append(run)

    tail = sorted(net)[:max(1, len(net) // 20)]  # worst 5%
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    expectancy = sum(net) / len(net)
    profit_factor = (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else float("inf")

    # Normalizer for comparing holding periods fairly: a 5-day hold that earns
    # 2x a 1-day hold is still worse per day of capital deployed.
    days_held = sum(getattr(r, "held_days", 1) for r in rows)

    # Capital actually put at risk. For a defined-risk structure the worst case is
    # the structural max loss, and positions are non-overlapping (one at a time),
    # so the peak single-position max loss is the capital the book must carry.
    # NOTE: this is the floor. Broker margin (SPAN+exposure) is higher — verify
    # against Zerodha before treating it as the real capital requirement.
    risks = [r.max_loss_structural for r in rows]
    capital_peak = max(risks) if risks else 0.0
    premium_total = sum(r.net_credit_per_lot * r.lots for r in rows)

    return {
        "underlying": underlying,
        "days": len(rows),
        "trades": len(rows),
        "days_held": days_held,
        "avg_hold_days": round(days_held / len(rows), 1),
        "net_per_day_held": round(sum(net) / days_held) if days_held else 0,
        "gross_per_day_held": round(sum(gross) / days_held) if days_held else 0,
        "capital_peak": round(capital_peak),
        "capital_median": round(statistics.median(risks)) if risks else 0,
        "premium_collected": round(premium_total),
        "return_on_capital_pct": (round(100 * sum(net) / capital_peak, 1)
                                  if capital_peak else None),
        "gross_total": round(sum(gross)),
        "charges_total": round(sum(charges)),
        "net_total": round(sum(net)),
        "cost_drag_pct_of_gross": round(100 * sum(charges) / sum(gross), 1) if sum(gross) else None,
        "win_rate": round(len(wins) / len(rows), 3),
        "avg_win": round(avg_win),
        "avg_loss": round(avg_loss),
        "expectancy_per_day": round(expectancy),
        "profit_factor": round(profit_factor, 2) if profit_factor != float("inf") else None,
        "best_day": round(max(net)),
        "worst_day": round(min(net)),
        "cvar_5pct": round(sum(tail) / len(tail)),
        "max_drawdown": round(_drawdown(equity)),
        "median_structural_max_loss": round(statistics.median(r.max_loss_structural for r in rows)),
    }


def print_report(strategy_name: str, results: list[DayResult],
                 skips: list[SkipDay], day_count: int, underlyings: list[str]):
    print(f"\n{'='*64}\n  {strategy_name} — backtest report")
    print(f"  {day_count} trading days scanned · {len(results)} traded · {len(skips)} skipped\n{'='*64}")
    for u in underlyings:
        s = summarize(results, u)
        if not s:
            print(f"\n  {u}: no tradeable days")
            continue
        print(f"\n  === {u} ({s['days']} days) ===")
        print(f"    Gross P&L:        ₹{s['gross_total']:>12,}")
        print(f"    Charges:          ₹{s['charges_total']:>12,}  ({s['cost_drag_pct_of_gross']}% of gross)")
        print(f"    NET P&L:          ₹{s['net_total']:>12,}   <-- the real number")
        print(f"    Win rate:         {s['win_rate']*100:.0f}%   PF: {s['profit_factor']}")
        print(f"    Expectancy/trade: ₹{s['expectancy_per_day']:>12,}  "
              f"(avg hold {s['avg_hold_days']}d)")
        print(f"    Per day held:     ₹{s['net_per_day_held']:>12,} net"
              f"  |  ₹{s['gross_per_day_held']:,} gross   <-- compare holds on THIS")
        print(f"    Avg win/loss:     ₹{s['avg_win']:,} / ₹{s['avg_loss']:,}")
        print(f"    Best/Worst day:   ₹{s['best_day']:,} / ₹{s['worst_day']:,}")
        print(f"    CVaR (worst 5%):  ₹{s['cvar_5pct']:,}")
        print(f"    Max drawdown:     ₹{s['max_drawdown']:,}")
        print(f"    Struct max loss:  ₹{s['median_structural_max_loss']:,} (median/round)")

    # Skip reasons breakdown — data-quality transparency.
    reasons: dict[str, int] = {}
    for sk in skips:
        reasons[sk.reason] = reasons.get(sk.reason, 0) + 1
    if reasons:
        print(f"\n  Skip reasons: " + ", ".join(f"{k} × {v}" for k, v in sorted(reasons.items(), key=lambda x: -x[1])))
    print()


def to_dicts(results: list[DayResult]) -> list[dict]:
    return [asdict(r) for r in results]
