"""Robustness diagnostics for backtest results.

These are deliberately separate from signal generation: they test whether a
strategy's edge survives adverse trade ordering and unseen data.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from services.backtest_service import BacktestConfig, run_backtest


def monte_carlo_trade_order(
    trades: list[dict[str, Any]],
    initial_capital: float,
    simulations: int = 2_000,
    ruin_fraction: float = 0.5,
    seed: int = 42,
) -> dict[str, Any]:
    """Shuffle historical trade returns to estimate path-dependent risk."""
    if not trades:
        return {"simulations": 0, "median_final_equity": initial_capital, "risk_of_ruin_pct": 0,
                "drawdown_percentiles": {"p50": 0, "p95": 0, "p99": 0}}
    if simulations < 100 or simulations > 20_000:
        raise ValueError("simulations must be between 100 and 20000")
    returns = np.array([float(t.get("net_pnl", 0)) for t in trades], dtype=float)
    rng = np.random.default_rng(seed)
    finals, drawdowns = [], []
    for _ in range(simulations):
        equity = float(initial_capital)
        peak = equity
        worst = 0.0
        for pnl in rng.permutation(returns):
            equity += pnl
            peak = max(peak, equity)
            worst = min(worst, equity / peak - 1)
        finals.append(equity)
        drawdowns.append(abs(worst) * 100)
    final_values = np.asarray(finals)
    dd_values = np.asarray(drawdowns)
    return {
        "simulations": simulations,
        "seed": seed,
        "median_final_equity": round(float(np.percentile(final_values, 50)), 2),
        "p05_final_equity": round(float(np.percentile(final_values, 5)), 2),
        "p95_final_equity": round(float(np.percentile(final_values, 95)), 2),
        "risk_of_ruin_pct": round(float((final_values <= initial_capital * ruin_fraction).mean() * 100), 4),
        "drawdown_percentiles": {"p50": round(float(np.percentile(dd_values, 50)), 4),
                                  "p95": round(float(np.percentile(dd_values, 95)), 4),
                                  "p99": round(float(np.percentile(dd_values, 99)), 4)},
    }


def walk_forward_analysis(data, config: BacktestConfig, train_bars: int, test_bars: int) -> dict[str, Any]:
    """Evaluate fixed parameters on sequential unseen test windows."""
    if train_bars < 3 or test_bars < 3:
        raise ValueError("train_bars and test_bars must each be at least 3")
    total = len(data)
    if total < train_bars + test_bars:
        raise ValueError("Not enough bars for one walk-forward window")
    windows = []
    start = 0
    while start + train_bars + test_bars <= total:
        train = data.iloc[start:start + train_bars] if hasattr(data, "iloc") else data[start:start + train_bars]
        test = data.iloc[start + train_bars:start + train_bars + test_bars] if hasattr(data, "iloc") else data[start + train_bars:start + train_bars + test_bars]
        # Training is reported for transparency; test results are the validation score.
        train_result = run_backtest(train, config)
        test_result = run_backtest(test, config)
        windows.append({"train_start": train.iloc[0]["timestamp"] if hasattr(train, "iloc") else train[0].get("timestamp"),
                        "test_start": test.iloc[0]["timestamp"] if hasattr(test, "iloc") else test[0].get("timestamp"),
                        "train_metrics": train_result["metrics"], "test_metrics": test_result["metrics"]})
        start += test_bars
    test_returns = [w["test_metrics"]["total_return_pct"] for w in windows]
    return {"window_count": len(windows), "profitable_windows": sum(value > 0 for value in test_returns),
            "test_return_pct": round(sum(test_returns), 4), "windows": windows}
