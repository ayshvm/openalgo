"""Small, deterministic OHLCV backtesting engine.

The engine deliberately has no broker or Flask dependencies so it can be used by
the API, notebooks, and strategy tests. Signals are evaluated on bar close and
orders fill on the next bar open (avoiding look-ahead bias).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class BacktestConfig:
    fast_period: int = 20
    slow_period: int = 50
    initial_capital: float = 100_000.0
    position_size_pct: float = 100.0
    commission_pct: float = 0.03
    slippage_pct: float = 0.01
    stop_loss_pct: float | None = None
    take_profit_pct: float | None = None
    max_holding_bars: int | None = None


def _number(value: Any, default: float | None = None) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clean_frame(data: pd.DataFrame | list[dict[str, Any]]) -> pd.DataFrame:
    frame = data.copy() if isinstance(data, pd.DataFrame) else pd.DataFrame(data)
    required = {"open", "high", "low", "close"}
    missing = required - {str(c).lower() for c in frame.columns}
    if missing:
        raise ValueError(f"Missing required OHLC columns: {', '.join(sorted(missing))}")
    frame.columns = [str(c).lower() for c in frame.columns]
    if "timestamp" not in frame:
        frame["timestamp"] = frame.index.astype(str)
    for column in required:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=list(required)).reset_index(drop=True)
    if len(frame) < 3:
        raise ValueError("At least 3 valid OHLC bars are required")
    return frame


def run_backtest(data: pd.DataFrame | list[dict[str, Any]], config: BacktestConfig | None = None) -> dict[str, Any]:
    """Run a long-only moving-average crossover backtest."""
    cfg = config or BacktestConfig()
    if cfg.fast_period < 2 or cfg.slow_period <= cfg.fast_period:
        raise ValueError("slow_period must be greater than fast_period >= 2")
    if cfg.initial_capital <= 0 or not 0 < cfg.position_size_pct <= 100:
        raise ValueError("initial_capital must be positive and position_size_pct must be in (0, 100]")

    frame = _clean_frame(data)
    frame["fast"] = frame["close"].ewm(span=cfg.fast_period, adjust=False).mean()
    frame["slow"] = frame["close"].ewm(span=cfg.slow_period, adjust=False).mean()
    frame["signal"] = (frame["fast"] > frame["slow"]).astype(int)

    cash = float(cfg.initial_capital)
    shares = 0.0
    entry_price = 0.0
    entry_bar = -1
    equity_curve: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    commission = max(0.0, cfg.commission_pct) / 100
    slippage = max(0.0, cfg.slippage_pct) / 100

    def mark_to_market(price: float) -> float:
        return cash + shares * price

    def close_position(bar: int, price: float, reason: str) -> None:
        nonlocal cash, shares, entry_price, entry_bar
        fill = price * (1 - slippage)
        gross = shares * (fill - entry_price)
        fees = shares * (entry_price + fill) * commission
        cash += shares * fill - shares * fill * commission
        trades.append({
            "entry_timestamp": frame.iloc[entry_bar]["timestamp"],
            "exit_timestamp": frame.iloc[bar]["timestamp"],
            "entry_price": round(entry_price, 6),
            "exit_price": round(fill, 6),
            "quantity": round(shares, 6),
            "gross_pnl": round(gross, 2),
            "fees": round(fees, 2),
            "net_pnl": round(gross - fees, 2),
            "return_pct": round((fill / entry_price - 1) * 100, 4),
            "bars_held": bar - entry_bar,
            "exit_reason": reason,
        })
        shares, entry_price, entry_bar = 0.0, 0.0, -1

    for i in range(len(frame)):
        row = frame.iloc[i]
        if shares:
            stop = cfg.stop_loss_pct is not None and row["low"] <= entry_price * (1 - cfg.stop_loss_pct / 100)
            target = cfg.take_profit_pct is not None and row["high"] >= entry_price * (1 + cfg.take_profit_pct / 100)
            timed_out = cfg.max_holding_bars is not None and i - entry_bar >= cfg.max_holding_bars
            if stop or target or timed_out or (i > entry_bar and frame.iloc[i - 1]["signal"] == 1 and row["signal"] == 0):
                close_position(i, float(row["open"] if i > entry_bar else row["close"]),
                               "stop_loss" if stop else "take_profit" if target else "max_holding" if timed_out else "signal")
        if not shares and i > 0 and frame.iloc[i - 1]["signal"] == 0 and row["signal"] == 1:
            fill = float(row["open"]) * (1 + slippage)
            budget = cash * cfg.position_size_pct / 100
            shares = budget / (fill * (1 + commission))
            cash -= shares * fill * (1 + commission)
            entry_price, entry_bar = fill, i
        equity_curve.append({"timestamp": row["timestamp"], "equity": round(mark_to_market(float(row["close"])), 2)})
    if shares:
        close_position(len(frame) - 1, float(frame.iloc[-1]["close"]), "end_of_data")
        equity_curve[-1]["equity"] = round(cash, 2)

    equity = pd.Series([point["equity"] for point in equity_curve], dtype=float)
    returns = equity.pct_change().replace([np.inf, -np.inf], np.nan).dropna()
    running_max = equity.cummax()
    drawdown = equity / running_max - 1
    net_pnl = cash - cfg.initial_capital
    wins = [t["net_pnl"] for t in trades if t["net_pnl"] > 0]
    losses = [t["net_pnl"] for t in trades if t["net_pnl"] < 0]
    periods = max(len(returns), 1)
    metrics = {
        "initial_capital": round(cfg.initial_capital, 2), "final_equity": round(cash, 2),
        "net_pnl": round(net_pnl, 2), "total_return_pct": round(net_pnl / cfg.initial_capital * 100, 4),
        "trade_count": len(trades), "win_rate_pct": round(len(wins) / len(trades) * 100, 2) if trades else 0,
        "profit_factor": round(sum(wins) / abs(sum(losses)), 4) if losses else (None if wins else 0),
        "expectancy": round(sum(t["net_pnl"] for t in trades) / len(trades), 2) if trades else 0,
        "max_drawdown_pct": round(abs(float(drawdown.min())) * 100, 4),
        "sharpe": round(float(returns.mean() / returns.std() * np.sqrt(periods)), 4) if len(returns) > 1 and returns.std() else 0,
        "sortino": round(float(returns.mean() / returns[returns < 0].std() * np.sqrt(periods)), 4) if len(returns[returns < 0]) > 1 and returns[returns < 0].std() else 0,
        "exposure_pct": round(sum(t["bars_held"] for t in trades) / len(frame) * 100, 2) if frame is not None else 0,
    }
    return {"metrics": metrics, "trades": trades, "equity_curve": equity_curve,
            "signals": frame[["timestamp", "close", "fast", "slow", "signal"]].round(6).to_dict("records")}
