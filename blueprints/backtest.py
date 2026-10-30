"""Session-authenticated backtesting endpoint backed by Historify data."""
from datetime import datetime, timezone
from flask import Blueprint, jsonify, request
from database.historify_db import get_ohlcv
from services.backtest_service import BacktestConfig, run_backtest
from utils.logging import get_logger
from utils.session import check_session_validity

logger = get_logger(__name__)
backtest_bp = Blueprint("backtest_bp", __name__, url_prefix="/backtest")

def _timestamp(value):
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=timezone.utc).timestamp())

@backtest_bp.route("/api/run", methods=["POST"])
@check_session_validity
def run():
    payload = request.get_json(silent=True) or {}
    try:
        data = payload.get("data")
        if data is None:
            symbol, exchange = payload.get("symbol"), payload.get("exchange")
            if not symbol or not exchange:
                return jsonify({"status": "error", "message": "Provide data or symbol and exchange"}), 400
            data = get_ohlcv(str(symbol).upper(), str(exchange).upper(), str(payload.get("interval", "D")), _timestamp(payload.get("start_date")), _timestamp(payload.get("end_date")))
        if hasattr(data, "to_dict"):
            data = data.to_dict("records")
        allowed = BacktestConfig.__dataclass_fields__
        config = BacktestConfig(**{key: value for key, value in (payload.get("config") or {}).items() if key in allowed})
        return jsonify({"status": "success", **run_backtest(data, config)})
    except (ValueError, TypeError) as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    except Exception:
        logger.exception("Backtest failed")
        return jsonify({"status": "error", "message": "Backtest failed"}), 500
