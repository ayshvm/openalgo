"""
Exact F&O options transaction-cost model (NSE index options).

WHY THIS MATTERS: your current backtest reports gross open-to-close P&L and
ignores costs. For a high-win-rate *seller*, costs are a large fraction of edge —
you pay STT on the sell side, brokerage per leg, exchange txn charges, GST,
stamp duty, and SEBI fees on every entry AND every exit (8 charged fills for a
4-leg condor round trip). A strategy that looks profitable gross can be flat or
negative net. Backtesting without this lies to you, especially at ₹10 Cr.

RATES ARE CONFIGURABLE AND DATED. Indian charges change (STT on options sale was
raised to 0.10% of premium effective 1-Oct-2024; exchange txn charges get revised
periodically). Treat the defaults below as a starting point and VERIFY against
Zerodha's current charge list / a contract note before trusting live sizing.
Last reviewed against public rates: 2025.

Everything is computed on *premium turnover* (price * qty), which is correct for
options (unlike futures, which use notional).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ChargeRates:
    # Brokerage: Zerodha F&O options = flat ₹20 per executed order (per leg fill).
    brokerage_per_order: float = 20.0
    # STT: 0.10% of premium, SELL side only (options). (raised from 0.0625% on 1-Oct-2024)
    stt_sell_pct: float = 0.0010
    # NSE exchange transaction charge on options premium (approx; verify current slab).
    exchange_txn_pct: float = 0.00035  # ~0.035% of premium
    # SEBI turnover fee: ₹10 per crore = 0.0001%.
    sebi_pct: float = 0.000001
    # NSE IPFT (investor protection fund): ~₹10 per crore on premium.
    ipft_pct: float = 0.000001
    # Stamp duty: 0.003% of premium, BUY side only.
    stamp_buy_pct: float = 0.00003
    # GST: 18% on (brokerage + exchange txn + sebi + ipft).
    gst_pct: float = 0.18


@dataclass
class LegFill:
    action: str      # "BUY" or "SELL"
    price: float
    qty: int         # total quantity (lots * lotsize)

    @property
    def turnover(self) -> float:
        return self.price * self.qty


def charge_leg(fill: LegFill, r: ChargeRates = ChargeRates()) -> dict:
    """Return itemized charges for a single option leg fill (one order)."""
    to = fill.turnover
    is_sell = fill.action.upper() == "SELL"
    is_buy = not is_sell

    brokerage = r.brokerage_per_order
    stt = to * r.stt_sell_pct if is_sell else 0.0
    exch = to * r.exchange_txn_pct
    sebi = to * r.sebi_pct
    ipft = to * r.ipft_pct
    stamp = to * r.stamp_buy_pct if is_buy else 0.0
    gst = (brokerage + exch + sebi + ipft) * r.gst_pct

    total = brokerage + stt + exch + sebi + ipft + stamp + gst
    return {
        "brokerage": brokerage, "stt": stt, "exchange_txn": exch,
        "sebi": sebi, "ipft": ipft, "stamp_duty": stamp, "gst": gst,
        "total": total,
    }


def round_trip_charges(entry_fills: list[LegFill], exit_fills: list[LegFill],
                       r: ChargeRates = ChargeRates()) -> dict:
    """Total costs for a full round trip (all entry legs + all exit legs).

    A 4-leg condor => 8 charged orders. Returns itemized totals + grand total.
    Pass exit fills at the price you actually exit (close/settlement in backtest)."""
    items = [charge_leg(f, r) for f in (entry_fills + exit_fills)]
    agg = {k: 0.0 for k in ("brokerage", "stt", "exchange_txn", "sebi", "ipft", "stamp_duty", "gst", "total")}
    for it in items:
        for k in agg:
            agg[k] += it[k]
    return agg
