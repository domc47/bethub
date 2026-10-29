#!/usr/bin/env python3
"""
low_trade_leaders.py

Find Polymarket leaderboard traders who made fewer than N trades (default 500)
in the last D days (default 30). Standard library only, so it drops straight
into a GitHub Actions job with no pip install.

Usage:
  python low_trade_leaders.py                      # top 100 by 30-day PnL, overall
  python low_trade_leaders.py --top 200 --category SPORTS
  python low_trade_leaders.py --max-trades 300 --days 14 --order VOL
"""

import argparse
import csv
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://data-api.polymarket.com"
CATEGORIES = ["OVERALL", "POLITICS", "SPORTS", "ESPORTS", "CRYPTO", "CULTURE",
              "MENTIONS", "WEATHER", "ECONOMICS", "TECH", "FINANCE"]
ACTIVITY_PAGE = 500  # server max per /activity request


def get(path, params, retries=4):
    url = f"{BASE}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "bethub-low-trade-scan/1.0"})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise
        except urllib.error.URLError:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise


def fetch_leaderboard(top, period, category, order):
    traders, offset = [], 0
    while len(traders) < top:
        batch = get("/v1/leaderboard", {
            "timePeriod": period, "category": category, "orderBy": order,
            "limit": min(50, top - len(traders)), "offset": offset,
        })
        if not batch:
            break
        traders.extend(batch)
        offset += len(batch)
        if offset > 1000:  # API offset cap
            break
    # de-dupe by wallet, keep best rank
    seen, out = set(), []
    for t in traders:
        w = t.get("proxyWallet")
        if w and w not in seen:
            seen.add(w)
            out.append(t)
    return out[:top]


def count_trades(wallet, since_ts, cap, return_rows=False):
    """Count TRADE rows since since_ts, stopping early once we hit cap.

    Keeps paging until the API returns nothing or we reach trades older than
    the window, so a server that returns smaller pages than requested can't
    cause an undercount.
    """
    total, offset, kept = 0, 0, []
    while total < cap:
        rows = get("/activity", {
            "user": wallet, "type": "TRADE", "start": since_ts,
            "limit": ACTIVITY_PAGE, "offset": offset,
            "sortBy": "TIMESTAMP", "sortDirection": "DESC",
        })
        if not rows:
            break
        in_window = [r for r in rows if int(r.get("timestamp") or 0) >= since_ts]
        total += len(in_window)
        if return_rows:
            kept.extend(in_window)
        if len(in_window) < len(rows):  # reached trades older than the window
            break
        offset += len(rows)
        if offset >= 5000:  # API offset cap
            break
    return (total, kept) if return_rows else total


def check_wallet(wallet, days, cap):
    """Debug one wallet: show the count and the date range it covers."""
    since = int(time.time()) - days * 86400
    n, rows = count_trades(wallet, since, cap, return_rows=True)
    fmt = lambda ts: time.strftime("%Y-%m-%d %H:%M", time.gmtime(int(ts)))
    print(f"Wallet {wallet}")
    print(f"Trades in last {days} days: {n if n < cap else str(cap) + '+'}")
    if rows:
        ts = [int(r.get("timestamp") or 0) for r in rows]
        print(f"Newest counted: {fmt(max(ts))} UTC")
        print(f"Oldest counted: {fmt(min(ts))} UTC")
        print(f"Window starts:  {fmt(since)} UTC")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--top", type=int, default=100, help="how many leaderboard traders to scan (max ~1000)")
    p.add_argument("--period", default="MONTH", choices=["DAY", "WEEK", "MONTH", "ALL"],
                   help="leaderboard time window")
    p.add_argument("--category", default="OVERALL", choices=CATEGORIES)
    p.add_argument("--order", default="PNL", choices=["PNL", "VOL"], help="rank leaderboard by PnL or volume")
    p.add_argument("--max-trades", type=int, default=500, help="keep traders with FEWER than this many trades")
    p.add_argument("--days", type=int, default=30, help="look-back window for counting trades")
    p.add_argument("--out", default="low_trade_leaders", help="output file prefix (.json and .csv written)")
    p.add_argument("--sleep", type=float, default=0.25, help="pause between traders to be polite to the API")
    p.add_argument("--wallet", help="check a single wallet address instead of scanning the leaderboard")
    a = p.parse_args()

    if a.wallet:
        check_wallet(a.wallet, a.days, a.max_trades)
        return

    since = int(time.time()) - a.days * 86400
    print(f"Pulling top {a.top} {a.category} traders by {a.order} ({a.period})...", file=sys.stderr)
    leaders = fetch_leaderboard(a.top, a.period, a.category, a.order)
    print(f"Got {len(leaders)}. Counting trades in last {a.days} days...", file=sys.stderr)

    keep = []
    for i, t in enumerate(leaders, 1):
        wallet = t["proxyWallet"]
        name = t.get("userName") or wallet[:10]
        try:
            n = count_trades(wallet, since, a.max_trades)
        except Exception as e:  # one bad wallet shouldn't kill the run
            print(f"  [{i}/{len(leaders)}] {name}: error {e}", file=sys.stderr)
            continue
        flag = "KEEP" if n < a.max_trades else f"{a.max_trades}+"
        print(f"  [{i}/{len(leaders)}] {name}: {n if n < a.max_trades else str(a.max_trades) + '+'} trades  {flag}",
              file=sys.stderr)
        if n < a.max_trades:
            keep.append({
                "rank": int(t.get("rank", 0) or 0),
                "username": t.get("userName"),
                "wallet": wallet,
                "trades_last_%dd" % a.days: n,
                "pnl": round(float(t.get("pnl") or 0), 2),
                "volume": round(float(t.get("vol") or 0), 2),
                "pnl_per_trade": round(float(t.get("pnl") or 0) / n, 2) if n else None,
                "x_username": t.get("xUsername"),
                "profile": f"https://polymarket.com/profile/{wallet}",
            })
        time.sleep(a.sleep)

    keep.sort(key=lambda r: r["rank"])
    with open(f"{a.out}.json", "w") as f:
        json.dump(keep, f, indent=2)
    if keep:
        with open(f"{a.out}.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(keep[0].keys()))
            w.writeheader()
            w.writerows(keep)

    print(f"\n{len(keep)} of {len(leaders)} traders made < {a.max_trades} trades in {a.days} days:\n")
    print(f"{'Rank':>4}  {'Trader':<24} {'Trades':>6} {'PnL':>14} {'PnL/trade':>11}")
    for r in keep:
        print(f"{r['rank']:>4}  {(r['username'] or r['wallet'][:12])[:24]:<24} "
              f"{r['trades_last_%dd' % a.days]:>6} {r['pnl']:>14,.0f} "
              f"{(r['pnl_per_trade'] or 0):>11,.0f}")
    print(f"\nSaved {a.out}.json and {a.out}.csv")


if __name__ == "__main__":
    main()
