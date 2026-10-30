import pandas as pd
from services.backtest_service import BacktestConfig, run_backtest

def test_backtest_reports_trades_and_metrics():
    closes = [10, 9, 8, 9, 11, 13, 12, 10]
    data = pd.DataFrame({"timestamp": range(len(closes)), "open": closes, "high": [x + 1 for x in closes], "low": [x - 1 for x in closes], "close": closes})
    result = run_backtest(data, BacktestConfig(fast_period=2, slow_period=3, commission_pct=0, slippage_pct=0))
    assert result["metrics"]["trade_count"] >= 1
    assert result["metrics"]["final_equity"] > 0
    assert all("exit_reason" in trade for trade in result["trades"])

def test_invalid_periods_are_rejected():
    data = [{"open": 1, "high": 2, "low": 0, "close": 1}] * 3
    try:
        run_backtest(data, BacktestConfig(fast_period=5, slow_period=3))
    except ValueError as exc:
        assert "slow_period" in str(exc)
    else:
        raise AssertionError("invalid periods should fail")
