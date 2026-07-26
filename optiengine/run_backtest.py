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
    args = ap.parse_args()

    strategy = IronCondor(short_offset=args.short_offset, hedge_offset=args.hedge_offset)
    cfg = BacktestConfig(
        underlyings=args.underlyings,
        start=date.today() - timedelta(days=args.days),
        end=date.today() - timedelta(days=1),
        lots=args.lots,
        cache_dir=args.cache_dir,
        slippage=SlippageModel(spread_pct=args.spread_pct),
        rates=ChargeRates(),
    )

    print(f"Backtesting {strategy.name} on {args.underlyings} "
          f"(OTM{args.hedge_offset} hedge / OTM{args.short_offset} short, {args.lots} lot(s))")
    results, skips, day_count = run(strategy, cfg)
    print_report(strategy.name, results, skips, day_count, args.underlyings)

    with open(args.out, "w") as f:
        json.dump({"results": to_dicts(results),
                   "skips": [s.__dict__ for s in skips]}, f, indent=2)
    print(f"Per-day results -> {args.out}")


if __name__ == "__main__":
    main()
