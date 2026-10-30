from services.backtest_analytics_service import monte_carlo_trade_order


def test_monte_carlo_is_repeatable_and_reports_risk():
    trades = [{"net_pnl": 100}, {"net_pnl": -50}, {"net_pnl": 25}]
    first = monte_carlo_trade_order(trades, 1000, simulations=100, seed=7)
    second = monte_carlo_trade_order(trades, 1000, simulations=100, seed=7)
    assert first == second
    assert "risk_of_ruin_pct" in first
    assert first["median_final_equity"] > 1000
