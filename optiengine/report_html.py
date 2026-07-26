"""Self-contained HTML backtest report.

Renders one or more backtest runs into a single offline HTML file: headline
stats, equity curve, per-trade P&L, return distribution, and the full trade
table. Charts are inline SVG — no CDN, no build step, no network. Open the file
directly in a browser.

Multiple runs render as a comparison (equity curves overlaid, stats side by
side), which is the point: the questions here are always "does A beat B".
"""
from __future__ import annotations

import html
import json
import os

from datetime import datetime

from .backtest.report import summarize

# Validated categorical slots (light / dark) — see dataviz reference palette.
SERIES = [("#2a78d6", "#3987e5"), ("#eb6834", "#d95926"), ("#1baf7a", "#199e70"),
          ("#eda100", "#c98500"), ("#e87ba4", "#d55181"), ("#008300", "#008300")]
POS, NEG = "#2a78d6", "#d03b3b"   # diverging poles: profit / loss


def _fmt(n: float | int | None, prefix: str = "₹") -> str:
    if n is None:
        return "—"
    return f"{prefix}{n:,.0f}"


def _esc(s) -> str:
    return html.escape(str(s))


# ---------------------------------------------------------------- charts ----

def _equity_chart(runs: list[dict], w: int = 860, h: int = 300) -> str:
    """Cumulative net P&L per run. X is trade sequence (runs differ in length)."""
    curves = []
    for r in runs:
        eq, run = [], 0.0
        for t in r["results"]:
            run += t["net_pnl"]
            eq.append(run)
        curves.append(eq)
    if not any(curves):
        return "<p class='empty'>No trades to plot.</p>"

    n = max(len(c) for c in curves) or 1
    lo = min([min(c) for c in curves if c] + [0.0])
    hi = max([max(c) for c in curves if c] + [0.0])
    span = (hi - lo) or 1.0
    pad_l, pad_r, pad_t, pad_b = 62, 14, 14, 26
    pw, ph = w - pad_l - pad_r, h - pad_t - pad_b

    def X(i):
        return pad_l + (i / max(n - 1, 1)) * pw

    def Y(v):
        return pad_t + (hi - v) / span * ph

    parts = [f'<svg viewBox="0 0 {w} {h}" class="chart" role="img" '
             f'aria-label="Cumulative net profit and loss by trade sequence">']

    # gridlines + y ticks
    for k in range(5):
        v = lo + span * k / 4
        y = Y(v)
        parts.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{w-pad_r}" y2="{y:.1f}" class="grid"/>')
        parts.append(f'<text x="{pad_l-8}" y="{y+4:.1f}" class="tick" text-anchor="end">{_fmt(v)}</text>')
    parts.append(f'<line x1="{pad_l}" y1="{Y(0):.1f}" x2="{w-pad_r}" y2="{Y(0):.1f}" class="zero"/>')

    for si, (r, eq) in enumerate(zip(runs, curves)):
        if not eq:
            continue
        light, dark = SERIES[si % len(SERIES)]
        d = " ".join(f"{'M' if i == 0 else 'L'}{X(i):.1f},{Y(v):.1f}" for i, v in enumerate(eq))
        parts.append(f'<path d="{d}" fill="none" stroke="{light}" class="line s{si}" '
                     f'style="--l:{light};--d:{dark}"/>')
        parts.append(f'<circle cx="{X(len(eq)-1):.1f}" cy="{Y(eq[-1]):.1f}" r="4" '
                     f'fill="{light}" class="dot s{si}" style="--l:{light};--d:{dark}"/>')

    parts.append(f'<text x="{pad_l}" y="{h-6}" class="tick">trade 1</text>')
    parts.append(f'<text x="{w-pad_r}" y="{h-6}" class="tick" text-anchor="end">trade {n}</text>')
    parts.append("</svg>")
    return "".join(parts)


def _pnl_bars(res: list[dict], w: int = 860, h: int = 200) -> str:
    """Per-trade P&L. Polarity encoding: profit and loss are opposite poles."""
    if not res:
        return "<p class='empty'>No trades.</p>"
    vals = [t["net_pnl"] for t in res]
    lo, hi = min(vals + [0.0]), max(vals + [0.0])
    span = (hi - lo) or 1.0
    pad_l, pad_r, pad_t, pad_b = 62, 14, 10, 22
    pw, ph = w - pad_l - pad_r, h - pad_t - pad_b
    bw = max(1.0, pw / len(vals) - 1.2)

    def Y(v):
        return pad_t + (hi - v) / span * ph

    y0 = Y(0)
    parts = [f'<svg viewBox="0 0 {w} {h}" class="chart" role="img" '
             f'aria-label="Net profit or loss per trade">']
    for k in range(3):
        v = lo + span * k / 2
        parts.append(f'<line x1="{pad_l}" y1="{Y(v):.1f}" x2="{w-pad_r}" y2="{Y(v):.1f}" class="grid"/>')
        parts.append(f'<text x="{pad_l-8}" y="{Y(v)+4:.1f}" class="tick" text-anchor="end">{_fmt(v)}</text>')

    for i, (t, v) in enumerate(zip(res, vals)):
        x = pad_l + i * (pw / len(vals))
        y = Y(v) if v >= 0 else y0
        bh = max(1.0, abs(Y(v) - y0))
        color = POS if v >= 0 else NEG
        tip = f'{t["date"]}  {_fmt(v)}'
        parts.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bw:.1f}" height="{bh:.1f}" '
                     f'rx="1.5" fill="{color}"><title>{_esc(tip)}</title></rect>')
    parts.append(f'<line x1="{pad_l}" y1="{y0:.1f}" x2="{w-pad_r}" y2="{y0:.1f}" class="zero"/>')
    parts.append("</svg>")
    return "".join(parts)


def _histogram(res: list[dict], w: int = 420, h: int = 200, bins: int = 21) -> str:
    if not res:
        return "<p class='empty'>No trades.</p>"
    vals = sorted(t["net_pnl"] for t in res)
    lo, hi = vals[0], vals[-1]
    if hi == lo:
        hi = lo + 1
    width = (hi - lo) / bins
    counts = [0] * bins
    for v in vals:
        counts[min(bins - 1, int((v - lo) / width))] += 1
    cmax = max(counts) or 1
    pad_l, pad_r, pad_t, pad_b = 34, 10, 10, 24
    pw, ph = w - pad_l - pad_r, h - pad_t - pad_b
    bw = pw / bins

    parts = [f'<svg viewBox="0 0 {w} {h}" class="chart" role="img" '
             f'aria-label="Distribution of per-trade profit and loss">']
    for i, c in enumerate(counts):
        bh = (c / cmax) * ph
        x = pad_l + i * bw
        y = pad_t + ph - bh
        lo_e, hi_e = lo + i * width, lo + (i + 1) * width
        color = POS if lo_e >= 0 else NEG
        parts.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{max(1.0, bw-2):.1f}" '
                     f'height="{max(0.0, bh):.1f}" rx="1.5" fill="{color}">'
                     f'<title>{_fmt(lo_e)} to {_fmt(hi_e)}: {c} trades</title></rect>')
    zx = pad_l + ((0 - lo) / (hi - lo)) * pw
    if pad_l <= zx <= w - pad_r:
        parts.append(f'<line x1="{zx:.1f}" y1="{pad_t}" x2="{zx:.1f}" y2="{pad_t+ph}" class="zero"/>')
    parts.append(f'<text x="{pad_l}" y="{h-6}" class="tick">{_fmt(lo)}</text>')
    parts.append(f'<text x="{w-pad_r}" y="{h-6}" class="tick" text-anchor="end">{_fmt(hi)}</text>')
    parts.append("</svg>")
    return "".join(parts)


# ---------------------------------------------------------------- layout ----

def _stat_tiles(s: dict, meta: dict) -> str:
    net = s.get("net_total", 0)
    roc = s.get("return_on_capital_pct")
    tiles = [
        ("Total earnings (net)", _fmt(net), "good" if net > 0 else "bad"),
        ("Capital used (peak)", _fmt(s.get("capital_peak")), ""),
        ("Return on capital", f"{roc:+.1f}%" if roc is not None else "—",
         "good" if (roc or 0) > 0 else "bad"),
        ("Per day held", _fmt(s.get("net_per_day_held")), "good" if s.get("net_per_day_held", 0) > 0 else "bad"),
        ("Premium collected", _fmt(s.get("premium_collected")), ""),
        ("Win rate", f"{s.get('win_rate', 0)*100:.0f}%", ""),
        ("Profit factor", f"{s.get('profit_factor')}", "good" if (s.get("profit_factor") or 0) > 1 else "bad"),
        ("Trades", f"{s.get('trades', 0)}", ""),
        ("Avg hold", f"{s.get('avg_hold_days', 1)}d", ""),
        ("Max drawdown", _fmt(s.get("max_drawdown")), "bad"),
        ("CVaR worst 5%", _fmt(s.get("cvar_5pct")), "bad"),
        ("Gross P&L", _fmt(s.get("gross_total")), ""),
        ("Charges", _fmt(s.get("charges_total")), ""),
        ("Capital per trade (median)", _fmt(s.get("capital_median")), ""),
    ]
    out = ['<div class="tiles">']
    for label, val, tone in tiles:
        out.append(f'<div class="tile"><div class="tl">{_esc(label)}</div>'
                   f'<div class="tv {tone}">{_esc(val)}</div></div>')
    out.append("</div>")
    return "".join(out)


def _compare_table(runs: list[dict]) -> str:
    rows = ['<table class="tbl"><thead><tr><th>Run</th><th>Trades</th><th>Avg hold</th>'
            '<th>Total earnings</th><th>Capital used</th><th>Return on capital</th>'
            '<th>Per day held</th><th>Win rate</th><th>PF</th>'
            '<th>Max DD</th></tr></thead><tbody>']
    for si, r in enumerate(runs):
        s = r["summary"]
        light, dark = SERIES[si % len(SERIES)]
        cls = "good" if s.get("net_total", 0) > 0 else "bad"
        roc = s.get("return_on_capital_pct")
        rows.append(
            f'<tr><td><span class="swatch" style="--l:{light};--d:{dark}"></span>'
            f'{_esc(r["meta"].get("label", "run"))}</td>'
            f'<td>{s.get("trades", 0)}</td><td>{s.get("avg_hold_days", 1)}d</td>'
            f'<td class="{cls}">{_fmt(s.get("net_total"))}</td>'
            f'<td>{_fmt(s.get("capital_peak"))}</td>'
            f'<td class="{cls}">{f"{roc:+.1f}%" if roc is not None else "—"}</td>'
            f'<td class="{cls}">{_fmt(s.get("net_per_day_held"))}</td>'
            f'<td>{s.get("win_rate", 0)*100:.0f}%</td><td>{s.get("profit_factor")}</td>'
            f'<td class="bad">{_fmt(s.get("max_drawdown"))}</td></tr>')
    rows.append("</tbody></table>")
    return "".join(rows)


CAPITAL_NOTE = (
    '<p class="note"><strong>Capital used</strong> is the peak structural max loss of a '
    'single position — positions never overlap, so that is the most the book can lose at '
    'once. It is a <em>floor</em>, not the real requirement: broker margin (SPAN + '
    'exposure) is higher, so verify against Zerodha before sizing. <strong>Return on '
    'capital</strong> is total earnings ÷ that peak, over the whole backtest window — '
    'capital is recycled once per trade, which is why the percentage looks large on a '
    'small base. Check the costs row below before reading any of it as profit.</p>')

# Which meta keys describe the strategy, and how to display them.
CONFIG_KEYS = [
    ("short_offset", "Short strike", lambda v: f"OTM {v}" if v is not None else "—"),
    ("hedge_offset", "Hedge strike", lambda v: f"OTM {v}" if v is not None else "—"),
    ("hold_days", "Hold", lambda v: f"{v} trading day{'s' if (v or 1) != 1 else ''}"),
    ("hold_to_expiry", "Hold to expiry", lambda v: "yes" if v else "no"),
    ("lots", "Lots", lambda v: f"{v}"),
    ("no_costs", "Costs applied", lambda v: "no — raw edge only" if v else "yes"),
    ("spread_pct", "Assumed spread", lambda v: f"{v:.2%}" if v is not None else "—"),
    ("days", "Lookback", lambda v: f"{v} calendar days"),
]


def _config_table(runs: list[dict]) -> str:
    """What actually differs between the runs — differences highlighted.

    Without this a comparison is unreadable: you see four equity curves and have
    to guess which knob moved.
    """
    head = ['<table class="tbl"><thead><tr><th>Parameter</th>']
    for si, r in enumerate(runs):
        light, dark = SERIES[si % len(SERIES)]
        head.append(f'<th><span class="swatch" style="--l:{light};--d:{dark}"></span>'
                    f'{_esc(r["meta"].get("label", "run"))}</th>')
    head.append("</tr></thead><tbody>")

    body = []
    for key, label, fmt in CONFIG_KEYS:
        vals = [r["meta"].get(key) for r in runs]
        if all(v is None for v in vals):
            continue
        differs = len({repr(v) for v in vals}) > 1
        cells = "".join(
            f'<td class="{"diff" if differs else ""}">{_esc(fmt(v))}</td>' for v in vals)
        body.append(f'<tr><td>{_esc(label)}{" ●" if differs else ""}</td>{cells}</tr>')
    body.append("</tbody></table>")
    note = ('<p class="note">● marks the parameters that differ between runs — '
            'everything else is held constant.</p>')
    return "".join(head) + "".join(body) + note


def _trade_table(res: list[dict], limit: int = 400) -> str:
    head = ('<table class="tbl"><thead><tr><th>Entry</th><th>Exit</th><th>Held</th>'
            '<th>Spot</th><th>Credit</th><th>Gross</th><th>Charges</th><th>Net</th>'
            '</tr></thead><tbody>')
    body = []
    for t in res[:limit]:
        cls = "good" if t["net_pnl"] > 0 else "bad"
        body.append(
            f'<tr><td>{_esc(t["date"])}</td><td>{_esc(t.get("exit_date") or t["date"])}</td>'
            f'<td>{t.get("held_days", 1)}d</td><td>{t["spot"]:,.0f}</td>'
            f'<td>{_fmt(t["net_credit_per_lot"])}</td><td>{_fmt(t["gross_pnl"])}</td>'
            f'<td>{_fmt(t["charges"])}</td><td class="{cls}">{_fmt(t["net_pnl"])}</td></tr>')
    tail = "</tbody></table>"
    more = (f'<p class="note">Showing first {limit} of {len(res)} trades.</p>'
            if len(res) > limit else "")
    return head + "".join(body) + tail + more


def _legend(runs: list[dict]) -> str:
    if len(runs) < 2:
        return ""
    items = []
    for si, r in enumerate(runs):
        light, dark = SERIES[si % len(SERIES)]
        items.append(f'<span class="lg"><span class="swatch" style="--l:{light};--d:{dark}">'
                     f'</span>{_esc(r["meta"].get("label", "run"))}</span>')
    return '<div class="legend">' + "".join(items) + "</div>"


CSS = """
:root{color-scheme:light dark}
*{box-sizing:border-box}
body{margin:0;background:var(--page);color:var(--ink);
  font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
.viz-root{--page:#f9f9f7;--surface:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;
  --muted:#898781;--grid:#e1e0d9;--axis:#c3c2b7;--ring:rgba(11,11,11,.10);
  --good:#006300;--bad:#d03b3b}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme=light])) .viz-root{
  --page:#0d0d0d;--surface:#1a1a19;--ink:#fff;--ink2:#c3c2b7;--muted:#898781;
  --grid:#2c2c2a;--axis:#383835;--ring:rgba(255,255,255,.10);--good:#0ca30c;--bad:#e66767}}
:root[data-theme=dark] .viz-root{--page:#0d0d0d;--surface:#1a1a19;--ink:#fff;
  --ink2:#c3c2b7;--muted:#898781;--grid:#2c2c2a;--axis:#383835;
  --ring:rgba(255,255,255,.10);--good:#0ca30c;--bad:#e66767}
.wrap{max-width:960px;margin:0 auto;padding:28px 20px 60px}
h1{font-size:20px;margin:0 0 4px}
h2{font-size:15px;margin:30px 0 10px;color:var(--ink)}
.sub{color:var(--ink2);margin:0 0 20px;font-size:13px}
.card{background:var(--surface);border:1px solid var(--ring);border-radius:10px;
  padding:16px;margin-bottom:16px;overflow-x:auto}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px}
.tile{background:var(--surface);border:1px solid var(--ring);border-radius:8px;padding:10px 12px}
.tl{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.04em}
.tv{font-size:19px;margin-top:3px;color:var(--ink)}
.tv.good{color:var(--good)}.tv.bad{color:var(--bad)}
.chart{width:100%;height:auto;display:block}
.grid{stroke:var(--grid);stroke-width:1}
.zero{stroke:var(--axis);stroke-width:1.5}
.tick{fill:var(--muted);font-size:10px;font-variant-numeric:tabular-nums}
.line{stroke-width:2;stroke-linejoin:round;stroke-linecap:round;stroke:var(--l)}
.dot{fill:var(--l)}
.tbl{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums;font-size:13px}
.tbl th{text-align:left;color:var(--muted);font-weight:600;font-size:11px;
  text-transform:uppercase;letter-spacing:.04em;padding:6px 10px;border-bottom:1px solid var(--ring)}
.tbl td{padding:6px 10px;border-bottom:1px solid var(--grid);color:var(--ink2)}
.tbl td.good{color:var(--good)}.tbl td.bad{color:var(--bad)}
.swatch{display:inline-block;width:10px;height:10px;border-radius:2px;
  background:var(--l);margin-right:7px;vertical-align:middle}
.legend{display:flex;flex-wrap:wrap;gap:16px;margin:0 0 10px;font-size:12px;color:var(--ink2)}
.note{color:var(--muted);font-size:12px;margin:8px 0 0}
.tbl td.diff{color:var(--ink);font-weight:600}
.tbl td.diff::after{content:"";display:inline-block}
.empty{color:var(--muted)}
details summary{cursor:pointer;color:var(--ink2);font-size:13px;padding:4px 0}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme=light])) .swatch,
  :root:where(:not([data-theme=light])) .line,
  :root:where(:not([data-theme=light])) .dot{--l:var(--d)}}
:root[data-theme=dark] .swatch,:root[data-theme=dark] .line,:root[data-theme=dark] .dot{--l:var(--d)}
"""


def render(runs: list[dict], out_path: str, title: str = "OptiEngine backtest") -> str:
    """runs: [{meta, results, skips, summary}] -> writes HTML, returns path."""
    primary = runs[0]
    generated = datetime.now().strftime("%d %b %Y %H:%M")

    body = [f'<div class="viz-root"><div class="wrap">',
            f"<h1>{_esc(title)}</h1>",
            f'<p class="sub">{_esc(primary["meta"].get("strategy", ""))} · '
            f'{_esc(", ".join(primary["meta"].get("underlyings", [])))} · '
            f'generated {generated}</p>']

    if len(runs) > 1:
        body.append("<h2>Comparison</h2><div class='card'>")
        body.append(_compare_table(runs))
        body.append(CAPITAL_NOTE)
        body.append("</div>")
        body.append("<h2>What differs between these runs</h2><div class='card'>")
        body.append(_config_table(runs))
        body.append("</div>")
    else:
        body.append(_stat_tiles(primary["summary"], primary["meta"]))
        body.append(f"<div class='card'>{CAPITAL_NOTE}</div>")
        body.append("<h2>Run configuration</h2><div class='card'>")
        body.append(_config_table(runs))
        body.append("</div>")

    body.append("<h2>Equity curve — cumulative net P&amp;L</h2><div class='card'>")
    body.append(_legend(runs))
    body.append(_equity_chart(runs))
    body.append("</div>")

    body.append(f"<h2>Per-trade P&amp;L — {_esc(primary['meta'].get('label',''))}</h2>"
                "<div class='card'>")
    body.append(_pnl_bars(primary["results"]))
    body.append("</div>")

    body.append("<h2>Distribution</h2><div class='card' style='max-width:460px'>")
    body.append(_histogram(primary["results"]))
    body.append("</div>")

    if len(runs) > 1:
        body.append("<h2>Headline stats — " + _esc(primary["meta"].get("label", "")) + "</h2>")
        body.append(_stat_tiles(primary["summary"], primary["meta"]))

    body.append("<h2>Trades</h2><details><summary>Show trade table "
                f"({len(primary['results'])} rows)</summary><div class='card'>")
    body.append(_trade_table(primary["results"]))
    body.append("</div></details>")

    skips = primary.get("skips", [])
    if skips:
        reasons: dict[str, int] = {}
        for s in skips:
            reasons[s.get("reason", "?")] = reasons.get(s.get("reason", "?"), 0) + 1
        body.append("<h2>Skipped days</h2><div class='card'><table class='tbl'>"
                    "<thead><tr><th>Reason</th><th>Count</th></tr></thead><tbody>")
        for k, v in sorted(reasons.items(), key=lambda x: -x[1]):
            body.append(f"<tr><td>{_esc(k)}</td><td>{v}</td></tr>")
        body.append("</tbody></table></div>")

    body.append("</div></div>")

    doc = (f"<!doctype html><html><head><meta charset='utf-8'>"
           f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
           f"<title>{_esc(title)}</title><style>{CSS}</style></head>"
           f"<body>{''.join(body)}</body></html>")

    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(doc)
    return os.path.abspath(out_path)


def load_run(path: str) -> dict:
    with open(path) as f:
        data = json.load(f)
    meta = data.get("meta", {"label": os.path.basename(path)})
    results = data.get("results", [])
    underlying = (meta.get("underlyings") or ["NIFTY"])[0]

    class _R:  # summarize() reads attributes
        pass

    objs = []
    for t in results:
        o = _R()
        o.__dict__.update(t)
        objs.append(o)
    summary = summarize(objs, underlying) if objs else {}
    return {"meta": meta, "results": results, "skips": data.get("skips", []),
            "summary": summary}


def render_from_files(paths: list[str], out: str = "auto",
                      title: str = "OptiEngine backtest") -> str:
    runs = [load_run(p) for p in paths]
    if out in (None, "auto"):
        out = os.path.join(os.path.dirname(__file__), "reports", "backtest.html")
    return render(runs, out, title)
