"""
Holiday-aware expiry calendar (2026 rules).

  NIFTY   weekly  -> Tuesday   (monthly = last Tuesday)
  SENSEX  weekly  -> Thursday  (BSE; monthly = last Thursday)
  BANKNIFTY/FINNIFTY/MIDCPNIFTY -> MONTHLY ONLY, last Tuesday
  Stock options   -> monthly, last Tuesday, PHYSICALLY SETTLED (exclude at scale)

If expiry falls on a holiday it shifts to the PREVIOUS trading day. Feed the NSE
trading-holiday list (dates) so `is_expiry` / `next_expiry` are accurate. Rules
have changed twice recently (Nov-2024 single-weekly, Sep-2025 NIFTY moved to Tue)
— keep this file as the single source of truth and update it if NSE/BSE revise.
"""
from __future__ import annotations

from datetime import date, timedelta

WEEKLY = {"NIFTY": 1, "SENSEX": 3}  # weekday(): Mon=0 .. Sun=6  -> Tue=1, Thu=3
MONTHLY_ONLY = {"BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"}
MONTHLY_WEEKDAY = {"BANKNIFTY": 1, "FINNIFTY": 1, "MIDCPNIFTY": 1,
                   "NIFTYNXT50": 1, "NIFTY": 1, "SENSEX": 3}


def _shift_for_holiday(d: date, holidays: set[date]) -> date:
    while d.weekday() >= 5 or d in holidays:
        d -= timedelta(days=1)
    return d


def has_weekly(underlying: str) -> bool:
    return underlying in WEEKLY


def next_weekly_expiry(underlying: str, ref: date, holidays: set[date] | None = None) -> date | None:
    if underlying not in WEEKLY:
        return None
    holidays = holidays or set()
    target = WEEKLY[underlying]
    d = ref
    for _ in range(14):
        if d.weekday() == target:
            return _shift_for_holiday(d, holidays)
        d += timedelta(days=1)
    return None


def last_weekday_of_month(year: int, month: int, weekday: int) -> date:
    if month == 12:
        d = date(year, 12, 31)
    else:
        d = date(year, month + 1, 1) - timedelta(days=1)
    while d.weekday() != weekday:
        d -= timedelta(days=1)
    return d


def monthly_expiry(underlying: str, year: int, month: int, holidays: set[date] | None = None) -> date:
    wd = MONTHLY_WEEKDAY.get(underlying, 1)
    return _shift_for_holiday(last_weekday_of_month(year, month, wd), holidays or set())
