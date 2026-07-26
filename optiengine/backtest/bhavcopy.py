"""
NSE F&O bhavcopy data layer — generalized from your backtest_iron_condor_daily.py.

Downloads/caches NSE's official daily settlement file (one row per contract per
day: O/H/L/C, settlement, underlying price, lot size) and exposes a clean
per-day chain the engine can resolve legs against. No broker connection needed.

Bhavcopy columns used: TckrSymb, XpryDt, FinInstrmTp (IDO=index option),
OptnTp (CE/PE), StrkPric, OpnPric, HghPric, LwPric, ClsPric, SttlmPric,
UndrlygPric, NewBrdLotQty.
"""
from __future__ import annotations

import csv
import io
import os
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import date, timedelta

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
BHAVCOPY_URL = ("https://nsearchives.nseindia.com/content/fo/"
                "BhavCopy_NSE_FO_0_0_0_{yyyymmdd}_F_0000.csv.zip")

# Exchange-fixed strike intervals (do NOT auto-detect — NSE lists intermediate
# strikes near spot that break a "most common gap" heuristic; see your original note).
STRIKE_INTERVALS = {
    "NIFTY": 50.0, "BANKNIFTY": 100.0, "FINNIFTY": 50.0,
    "MIDCPNIFTY": 25.0, "NIFTYNXT50": 100.0,
}


@dataclass
class OptionQuote:
    open: float
    high: float
    low: float
    close: float
    settle: float
    lotsize: int


class DayChain:
    """All option rows for one underlying+expiry on one day, indexed by (strike, type).

    `spot` MUST be the entry-time (open) underlying level — see spot_at_open().
    `close_spot` is end-of-day and is for diagnostics only; using it to pick
    strikes reintroduces lookahead bias.
    """

    def __init__(self, underlying: str, expiry: str, spot: float, rows: list[dict]):
        self.underlying = underlying
        self.expiry = expiry
        self.spot = spot            # at OPEN — knowable at entry
        self.close_spot: float | None = None  # diagnostics only
        self.interval = STRIKE_INTERVALS.get(underlying, 50.0)
        self.atm = round(spot / self.interval) * self.interval
        self._by_key: dict[tuple[float, str], dict] = {}
        for r in rows:
            self._by_key[(round(float(r["StrkPric"]), 2), r["OptnTp"])] = r

    def strike_for_offset(self, offset: int) -> float:
        return self.atm + offset * self.interval

    def quote_at_offset(self, offset: int, option_type: str) -> OptionQuote | None:
        strike = self.strike_for_offset(offset)
        row = self._by_key.get((round(strike, 2), option_type))
        if row is None:
            return None
        return self._parse(row)

    @staticmethod
    def _parse(row: dict) -> OptionQuote:
        o = float(row["OpnPric"]); h = float(row["HghPric"])
        lo = float(row["LwPric"]); c = float(row["ClsPric"])
        s = float(row["SttlmPric"])
        if o <= 0: o = s
        if c <= 0: c = s
        if h <= 0: h = max(o, c)
        if lo <= 0: lo = min(o, c) if min(o, c) > 0 else c
        return OptionQuote(o, h, lo, c, s, int(float(row["NewBrdLotQty"])))


def fetch_bhavcopy(day: date, cache_dir: str) -> list[dict] | None:
    """Return list of dict rows for one trading day, or None (holiday/weekend)."""
    os.makedirs(cache_dir, exist_ok=True)
    cache_path = os.path.join(cache_dir, f"{day.isoformat()}.csv")
    if os.path.exists(cache_path):
        with open(cache_path, newline="") as f:
            return list(csv.DictReader(f))

    url = BHAVCOPY_URL.format(yyyymmdd=day.strftime("%Y%m%d"))
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise

    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        with zf.open(zf.namelist()[0]) as f:
            rows = list(csv.DictReader(io.TextIOWrapper(f, encoding="utf-8")))

    with open(cache_path, "w", newline="") as f:
        if rows:
            w = csv.DictWriter(f, fieldnames=rows[0].keys())
            w.writeheader()
            w.writerows(rows)
    return rows


def nearest_expiry(rows: list[dict], underlying: str, trade_day: date) -> str | None:
    cands = [r["XpryDt"] for r in rows
             if r["TckrSymb"] == underlying and r["FinInstrmTp"] == "IDO"
             and r["XpryDt"] >= trade_day.isoformat()]
    return min(cands) if cands else None


# NSE index bhavcopy: true index OHLC (the underlying itself, no futures basis).
INDEX_URL = ("https://nsearchives.nseindia.com/content/indices/"
             "ind_close_all_{ddmmyyyy}.csv")
INDEX_NAMES = {
    "NIFTY": "nifty 50",
    "BANKNIFTY": "nifty bank",
    "FINNIFTY": "nifty financial services",
    "MIDCPNIFTY": "nifty midcap select",
    "NIFTYNXT50": "nifty next 50",
}


def fetch_index_opens(day: date, cache_dir: str) -> dict[str, float]:
    """{index name (lowercased) -> OPEN} for one day, from NSE's index bhavcopy.

    This is the correct anchor for strike selection: the real index open, with no
    futures basis and no knowledge of the close.
    """
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, f"idx-{day.isoformat()}.csv")
    if not os.path.exists(path):
        url = INDEX_URL.format(ddmmyyyy=day.strftime("%d%m%Y"))
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return {}
            raise
        with open(path, "wb") as f:
            f.write(raw)

    out: dict[str, float] = {}
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        for r in csv.DictReader(f):
            name = (r.get("Index Name") or "").strip().lower()
            try:
                o = float(r.get("Open Index Value") or 0)
            except ValueError:
                continue
            if name and o > 0:
                out[name] = o
    return out


def spot_at_open(rows: list[dict], underlying: str) -> float | None:
    """Underlying level as known AT ENTRY TIME (market open).

    CRITICAL — do not substitute `UndrlygPric` here. That column is the day's
    CLOSING underlying level, so centring strikes on it means selecting strikes
    with knowledge of where the day ended: lookahead bias that manufactures a
    fake edge (measured: it lifted win rate to 72% with avg win > avg loss,
    which no defined-risk seller actually gets).

    We use the near-month index FUTURE's open as the entry-time proxy. It carries
    a small basis vs spot, but it is genuinely knowable at 9:15 — which is the
    property that matters. Live, this same value comes from the OpenAlgo quote
    feed at entry.
    """
    futs = [r for r in rows
            if r["TckrSymb"] == underlying and r["FinInstrmTp"] == "IDF"]
    if not futs:
        return None
    near = min(futs, key=lambda r: r["XpryDt"])
    try:
        o = float(near["OpnPric"])
    except (TypeError, ValueError):
        return None
    return o if o > 0 else None


def build_day_chain(rows: list[dict], underlying: str, trade_day: date,
                    index_opens: dict[str, float] | None = None) -> DayChain | None:
    expiry = nearest_expiry(rows, underlying, trade_day)
    if expiry is None:
        return None
    opt_rows = [r for r in rows
                if r["TckrSymb"] == underlying and r["XpryDt"] == expiry
                and r["FinInstrmTp"] == "IDO"]
    if not opt_rows:
        return None
    # Preferred anchor: the true index open. Fallback: near-month future's open
    # (carries basis, but is still knowable at entry). Never the closing price.
    spot = None
    if index_opens:
        spot = index_opens.get(INDEX_NAMES.get(underlying, "").lower())
    if spot is None:
        spot = spot_at_open(rows, underlying)
    if spot is None:
        # No future to anchor on -> we cannot pick strikes without lookahead.
        # Skip the day rather than silently fall back to the closing price.
        return None
    close_spot = float(opt_rows[0]["UndrlygPric"])  # diagnostics only, never for strikes
    chain = DayChain(underlying, expiry, spot, opt_rows)
    chain.close_spot = close_spot
    return chain


def trading_days(start: date, end: date):
    d = start
    while d <= end:
        if d.weekday() < 5:
            yield d
        d += timedelta(days=1)
