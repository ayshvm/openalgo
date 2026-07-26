#!/usr/bin/env python
"""Build an HTML report from one or more backtest result JSONs and open it.

  python -m optiengine.make_report --open                    # every run found
  python -m optiengine.make_report /tmp/h_1.json /tmp/h_5.json --open
  python -m optiengine.make_report --glob '/tmp/h_*.json' --title 'Holding period'

With several inputs the report renders as a comparison: equity curves overlaid
and stats side by side.
"""
from __future__ import annotations

import argparse
import glob as globlib
import os
import subprocess
import sys

from .report_html import render_from_files

DEFAULT_GLOB = os.path.join(os.path.dirname(__file__), "backtest_out*.json")


def open_in_browser(path: str) -> None:
    try:
        if sys.platform == "darwin":
            subprocess.run(["open", path], check=False)
        elif os.name == "nt":
            os.startfile(path)  # noqa: S606
        else:
            subprocess.run(["xdg-open", path], check=False)
    except Exception as e:  # never fail the report over this
        print(f"(could not auto-open: {e})")


def main():
    ap = argparse.ArgumentParser(description="Render backtest results as HTML")
    ap.add_argument("paths", nargs="*", help="result JSON files")
    ap.add_argument("--glob", default=None, help="glob pattern for result JSONs")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(__file__),
                                                  "reports", "backtest.html"))
    ap.add_argument("--title", default="OptiEngine backtest")
    ap.add_argument("--open", action="store_true", help="open in the default browser")
    args = ap.parse_args()

    paths = list(args.paths)
    if args.glob:
        paths += sorted(globlib.glob(args.glob))
    if not paths:
        paths = sorted(globlib.glob(DEFAULT_GLOB))
    if not paths:
        print("No result JSONs found. Run a backtest first:\n"
              "  python -m optiengine.run_backtest --underlyings NIFTY --days 365")
        raise SystemExit(1)

    out = render_from_files(paths, args.out, args.title)
    print(f"Report ({len(paths)} run{'s' if len(paths) > 1 else ''}) -> {out}")
    if args.open:
        open_in_browser(out)


if __name__ == "__main__":
    main()
