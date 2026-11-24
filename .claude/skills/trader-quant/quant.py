#!/usr/bin/env python3
"""trader-quant: a 60-day trend signal and its backtest, on Yahoo daily closes.

Signal: today's close vs the average close of the previous N (60) sessions.
    above -> BUY, below -> SELL (long side: exit / stay out; no shorting).
Also prints where today's close ranks among those N closes (percentile).

Backtest: the same rule on every past day, holding the next day if BUY, in cash if
SELL, with a cost on every switch. Compared with simply holding.

    python3 quant.py TSLA XMR-USD
    python3 quant.py TSLA --window 60 --cost-bps 5 --json
    python3 quant.py TSLA --offline TSLA-2y.json    # a saved Yahoo chart JSON
"""

import argparse
import json
import math
import sys
import time
import urllib.error
import urllib.request

UA = "Mozilla/5.0 (compatible; day-trader-skill/2.0; +https://github.com/Jakeschincariol/day-trader-skill)"


def fetch_closes(sym, offline=None):
    """Daily (date, close) pairs, oldest first. query2 first, query1 on 429/5xx."""
    if offline:
        with open(offline, encoding="utf-8") as fh:
            data = json.load(fh)
    else:
        data = None
        for host in ("query2", "query1"):
            url = f"https://{host}.finance.yahoo.com/v8/finance/chart/{sym}?range=2y&interval=1d"
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=30) as r:
                    data = json.load(r)
                break
            except urllib.error.HTTPError as e:
                if e.code != 429 and e.code < 500:
                    raise SystemExit(f"{sym}: Yahoo answered {e.code}")
                time.sleep(1)
        if data is None:
            raise SystemExit(f"{sym}: Yahoo did not answer")
    res = (data.get("chart") or {}).get("result")
    if not res:
        raise SystemExit(f"{sym}: no chart data (unknown ticker?)")
    res = res[0]
    closes = res["indicators"]["quote"][0]["close"]
    return [(time.strftime("%Y-%m-%d", time.gmtime(t)), c) for t, c in zip(res["timestamp"], closes) if c is not None]


def signal(closes, window=60):
    """Today's close vs the previous `window` closes. Needs window + 1 closes."""
    if len(closes) < window + 1:
        raise ValueError(f"need {window + 1} closes, have {len(closes)}")
    today, past = closes[-1], closes[-window - 1:-1]
    avg = sum(past) / window
    pct = (today / avg - 1) * 100
    rank = sum(c <= today for c in past) / window * 100
    return {"close": today, "avg": avg, "pct_vs_avg": pct, "percentile": rank,
            "signal": "BUY" if pct > 0 else "SELL" if pct < 0 else "FLAT"}


def backtest(closes, window=60, cost_bps=5.0):
    """Long the next day when the rule says BUY, cash otherwise. Cost on each switch."""
    strat, held, switches, in_market, peak, max_dd, pos = 1.0, 1.0, 0, 0, 1.0, 0.0, 0
    days = 0
    for t in range(window, len(closes) - 1):
        want = 1 if signal(closes[: t + 1], window)["signal"] == "BUY" else 0
        if want != pos:
            strat *= 1 - cost_bps / 10000
            switches += 1
            pos = want
        r = closes[t + 1] / closes[t]
        held *= r
        if pos:
            strat *= r
            in_market += 1
        peak = max(peak, strat)
        max_dd = min(max_dd, strat / peak - 1)
        days += 1
    return {"days": days, "strategy_pct": (strat - 1) * 100, "hold_pct": (held - 1) * 100,
            "switches": switches, "in_market_pct": in_market / days * 100 if days else 0.0,
            "max_drawdown_pct": max_dd * 100}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("tickers", nargs="+")
    p.add_argument("--window", type=int, default=60)
    p.add_argument("--cost-bps", type=float, default=5.0, help="cost per switch in basis points (default 5)")
    p.add_argument("--offline", help="a saved Yahoo chart JSON (one ticker)")
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    out = []
    for sym in a.tickers:
        rows = fetch_closes(sym, a.offline)
        closes = [c for _, c in rows]
        s = signal(closes, a.window)
        out.append({"symbol": sym, "date": rows[-1][0], **s, "backtest": backtest(closes, a.window, a.cost_bps)})
    if a.json:
        print(json.dumps(out, indent=2))
        return 0
    for o in out:
        b = o["backtest"]
        print(f"{o['symbol']:<9} {o['signal']:<4}  close {o['close']:.2f} on {o['date']}, {o['pct_vs_avg']:+.2f}% vs its "
              f"{a.window}-day average {o['avg']:.2f}; above {o['percentile']:.0f}% of the last {a.window} closes")
        print(f"          backtest {b['days']} days: rule {b['strategy_pct']:+.1f}% vs holding {b['hold_pct']:+.1f}%, "
              f"{b['switches']} switches at {a.cost_bps:g} bps, in the market {b['in_market_pct']:.0f}% of days, "
              f"worst drawdown {b['max_drawdown_pct']:.1f}%")
    print("Daily closes from Yahoo (free, unofficial; today's bar may still be moving). A signal is not an order. Not financial advice.")
    return 0


if __name__ == "__main__":
    # Self-check on a made-up series: rising -> BUY at 100th percentile, falling -> SELL.
    up = [100 + i for i in range(61)]
    assert signal(up)["signal"] == "BUY" and signal(up)["percentile"] == 100
    assert signal(up[::-1])["signal"] == "SELL" and signal(up[::-1])["percentile"] == 0
    flat_hold = backtest([100.0] * 70, cost_bps=0)
    assert math.isclose(flat_hold["hold_pct"], 0) and flat_hold["switches"] == 0
    sys.exit(main())
