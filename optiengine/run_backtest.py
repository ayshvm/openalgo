#!/usr/bin/env python
"""
CLI: backtest a strategy over NSE bhavcopy. Offline — no broker needed.

Examples:
  uv run python -m optiengine.run_backtest --underlyings NIFTY --days 120
  uv run python -m optiengine.run_backtest --underlyings NIFTY BANKNIFTY --days 90 --lots 5

Reports GROSS vs NET (after exact charges + size-aware slippage) so you can see
how much edge the costs eat — the number that matters at scale.
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import date, timedelta

from .backtest.engine import BacktestConfig, run
from .backtest.fills import SlippageModel
from .backtest.multiday import run_multiday
from .backtest.report import print_report, to_dicts
from .charges import ChargeRates
from .strategies.iron_condor import IronCondor


def main():
    ap = argparse.ArgumentParser(description="OptiEngine backtest")
    ap.add_argument("--underlyings", nargs="+", default=["NIFTY"])
    ap.add_argument("--days", type=int, default=120, help="lookback trading window in calendar days")
    ap.add_argument("--lots", type=int, default=1)
    ap.add_argument("--short-offset", type=int, default=4)
    ap.add_argument("--hedge-offset", type=int, default=6)
    ap.add_argument("--cache-dir", default=os.path.join(os.path.dirname(__file__), ".bhavcopy_cache"))
    ap.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "backtest_out.json"))
    ap.add_argument("--spread-pct", type=float, default=0.01)
    ap.add_argument("--hold-days", type=int, default=1,
                    help="trading days to hold (1 = same-day round trip)")
    ap.add_argument("--hold-to-expiry", action="store_true",
                    help="hold until the expiry day's close (overrides --hold-days)")
    ap.add_argument("--no-costs", action="store_true",
                    help="zero out charges AND slippage — measures the raw structural "
                         "edge only. Use when the real cost depends on a rebalancing "
                         "frequency you have not fixed yet.")
    args = ap.parse_args()

    strategy = IronCondor(short_offset=args.short_offset, hedge_offset=args.hedge_offset)
    if args.no_costs:
        slippage = SlippageModel(spread_pct=0.0, impact_coef=0.0, min_ticks=0.0)
        rates = ChargeRates(brokerage_per_order=0.0, stt_sell_pct=0.0,
                            exchange_txn_pct=0.0, sebi_pct=0.0, ipft_pct=0.0,
                            stamp_buy_pct=0.0, gst_pct=0.0)
    else:
        slippage = SlippageModel(spread_pct=args.spread_pct)
        rates = ChargeRates()

    cfg = BacktestConfig(
        underlyings=args.underlyings,
        start=date.today() - timedelta(days=args.days),
        end=date.today() - timedelta(days=1),
        lots=args.lots,
        cache_dir=args.cache_dir,
        slippage=slippage,
        rates=rates,
    )

    mode = ("hold-to-expiry" if args.hold_to_expiry
            else f"{args.hold_days}-day hold" if args.hold_days > 1 else "same-day")
    print(f"Backtesting {strategy.name} on {args.underlyings} "
          f"(OTM{args.hedge_offset} hedge / OTM{args.short_offset} short, "
          f"{args.lots} lot(s), {mode}{', NO COSTS' if args.no_costs else ''})")

    if args.hold_to_expiry or args.hold_days > 1:
        results, skips, day_count = run_multiday(
            strategy, cfg, hold_days=args.hold_days, hold_to_expiry=args.hold_to_expiry)
    else:
        results, skips, day_count = run(strategy, cfg)
    print_report(strategy.name, results, skips, day_count, args.underlyings)

    with open(args.out, "w") as f:
        json.dump({"results": to_dicts(results),
                   "skips": [s.__dict__ for s in skips]}, f, indent=2)
    print(f"Per-day results -> {args.out}")


if __name__ == "__main__":
    main()
