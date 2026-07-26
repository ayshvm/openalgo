# OptiEngine — strategy + backtest + selector engine

Phase 0 of the autonomous options-selling system. Lives inside the OpenAlgo repo
(`openalgo/optiengine/`) and runs on the same box. This phase is **offline** — it
needs no broker connection, only NSE's public bhavcopy (same source your existing
`strategies/examples/backtest_iron_condor_daily.py` uses).

See the full design in the Trading project doc *"Autonomous Options-Selling
System — Build Plan"*.

## What's here (Phase 0)

```
optiengine/
├── strategies/
│   ├── base.py         # Strategy interface — one contract for backtest AND live
│   └── iron_condor.py  # your iron_condor_daily.py refactored into the interface
├── charges.py          # EXACT F&O cost model (STT/brokerage/txn/GST/stamp/SEBI)
├── backtest/
│   ├── bhavcopy.py     # NSE bhavcopy loader + per-day option chain
│   ├── fills.py        # size-aware slippage (spread + impact) — always adverse
│   ├── engine.py       # runs any Strategy over history; GROSS and NET per day
│   └── report.py       # metrics: win rate, expectancy, drawdown, CVaR, cost drag
├── selector/
│   ├── features.py     # regime features (expected vs realized move = VRP)
│   └── policy.py       # rules-based "which strategy today?" (white-box)
├── calendar.py         # 2026 expiry rules (NIFTY Tue / SENSEX Thu; holiday-aware)
├── run_backtest.py     # CLI
└── config/strategies.yaml
```

## What Phase 0 adds over your current backtester

1. **Exact charges** — your backtest reports gross P&L only. For a high-win-rate
   seller, STT (0.10% sell-side) + brokerage + txn + GST + stamp eat a large slice
   of edge across 8 round-trip fills. The report shows NET and the **cost drag %**.
2. **Size-aware slippage** — real fills cross the spread and move the book at
   ₹10 Cr size. Modeled as an always-adverse haircut that grows with lots.
3. **Pluggable Strategy interface** — the *same* object will trade live in Phase 2.
   Iron condor is the reference implementation; add iron fly / strangle / calendar
   by implementing the same 6 methods.
4. **Regime selector** — the "right strategy for the day/week" brain, rules-based
   for now (trustworthy), VRP-driven.

## Run it

```bash
cd openalgo
python3.11 -m optiengine.run_backtest --underlyings NIFTY --days 365
python3.11 -m optiengine.run_backtest --underlyings NIFTY --days 365 --hold-days 3
python3.11 -m optiengine.run_backtest --underlyings NIFTY --days 365 --no-costs --html
```

Useful flags: `--short-offset/--hedge-offset` (strike distance), `--lots`,
`--spread-pct`, `--hold-days N` / `--hold-to-expiry`, `--no-costs`, `--html`.

The first run downloads + caches bhavcopy and the NSE index file into
`optiengine/.bhavcopy_cache/` (~1.6 GB for a year; gitignored). Results go to
`optiengine/backtest_out.json`.

## View results in a browser

```bash
python3.11 -m optiengine.make_report --open                      # all runs found
python3.11 -m optiengine.make_report /tmp/a.json /tmp/b.json --open   # compare runs
```

Writes a self-contained HTML file to `optiengine/reports/` — headline stats,
equity curve, per-trade P&L, distribution, trade table, skip reasons. No network
or build step; opens from `file://`. Passing multiple result files renders a
comparison with the equity curves overlaid.

## Findings so far (NIFTY, 240 trading days to Jul-2026)

1. **Strike selection must anchor on the index OPEN.** The F&O bhavcopy
   `UndrlygPric` column is the day's CLOSE. Centring strikes on it while entering
   at the open leaks the outcome into the trade: it produced a 72% win rate with
   avg win > avg loss — a shape no defined-risk seller gets. Fixing it flipped the
   same 240 days from +₹136,688 to −₹67,913 per lot.
2. **The same-day iron condor has no edge after costs.** Per lot per day:
   frictionless edge ≈ +₹108, slippage ≈ −₹185, charges ≈ −₹211 → net ≈ −₹287.
   Friction is ~3.7× the raw edge. Every strike configuration from OTM2/4 to
   OTM10/12 loses money.
3. **Holding longer does not rescue it.** Costs amortize over more days (the loss
   per day held shrinks from −₹132 to −₹49), but the raw edge per day of capital
   decays faster than the saving — ₹184/day held at a 1-day hold vs ₹62 at 5 days,
   with drawdown roughly doubling. Nothing tested reaches positive.
4. **Liquidity is not the constraint.** The thinnest leg (OTM6 PE) trades ~21,500
   lots/day, so a risk-capped position is ~1% participation. The binding
   constraint is the risk budget, not depth.

## Honesty about data limits (unchanged from your original stance)

Bhavcopy is EOD (one O/H/L/C per contract). The trustworthy simulation is
**enter-at-open → exit-at-close**; it does **not** fake intraday stop/target
triggers from four independent daily H/L ranges. Days with unreliable prices
(credit ≤ 0, credit > width, missing strike) are skipped and counted, never traded.
For intraday-accurate backtests, start recording your OpenAlgo WebSocket option
snapshots to DuckDB now (Phase 0.5) — that captured history becomes your moat.

## Verify before trusting live sizing

Charge rates in `charges.py` are dated (reviewed 2025) and configurable. STT on
options sale rose to 0.10% on 1-Oct-2024; exchange txn charges get revised. Check
them against a current Zerodha contract note before Phase 2.

## Next (Phase 1)

`live/` (executor + intraday evaluator against OpenAlgo API) and `risk/` (guardian
+ kill switch). Config and limits already stubbed in `config/strategies.yaml`.
