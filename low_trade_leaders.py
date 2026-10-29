#!/usr/bin/env python3
"""
low_trade_leaders.py

Find Polymarket leaderboard traders who made fewer than N actions (default 500)
in the last D days (default 30), then check whether copying ONLY their buys and
sells (ignoring converts, merges and splits) would have made money.
Standard library only, so it drops straight into a GitHub Actions job.

Usage:
  python low_trade_leaders.py                      # top 100 by 30-day PnL, overall
  python low_trade_leaders.py --top 200 --category SPORTS
  python low_trade_leaders.py --max-trades 300 --days 14 --order VOL
  python low_trade_leaders.py --min-roi 0          # only traders whose buys/sells made money
  python low_trade_leaders.py --wallet 0xABC...    # check one trader
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
GAMMA = "https://gamma-api.polymarket.com"
CATEGORIES = ["OVERALL", "POLITICS", "SPORTS", "ESPORTS", "CRYPTO", "CULTURE",
              "MENTIONS", "WEATHER", "ECONOMICS", "TECH", "FINANCE"]
ACTIVITY_PAGE = 500  # server max per /activity request
# Activity types that count as the trader actively doing something.
# CONVERSION = the "Convert" rows on a profile (multi-outcome markets).
# REDEEM and REWARD are left out because they're payouts, not bets.
DEFAULT_TYPES = "TRADE,CONVERSION,SPLIT,MERGE"


def get(path, params, retries=4, base=BASE):
    url = f"{base}{path}?{urllib.parse.urlencode(params, doseq=True)}"
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


def count_trades(wallet, since_ts, cap, return_rows=False, types=DEFAULT_TYPES):
    """Count TRADE rows since since_ts, stopping early once we hit cap.

    Keeps paging until the API returns nothing or we reach trades older than
    the window, so a server that returns smaller pages than requested can't
    cause an undercount.
    """
    total, offset, kept = 0, 0, []
    while total < cap:
        rows = get("/activity", {
            "user": wallet, "type": types, "start": since_ts,
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


def type_breakdown(rows):
    out = {}
    for r in rows:
        t = r.get("type") or "UNKNOWN"
        out[t] = out.get(t, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def _json_list(v):
    if isinstance(v, list):
        return v
    try:
        return json.loads(v or "[]")
    except (TypeError, ValueError):
        return []


def fetch_markets(condition_ids, batch=20):
    """conditionId -> {"prices": [...], "tokens": [...], "closed": bool} from the Gamma API.

    Resolved markets show prices of 1 (winner) and 0 (loser); open markets
    show the current price, so open positions are valued at today's price.
    """
    out, ids = {}, sorted(set(c for c in condition_ids if c))

    def pull(chunk, extra):
        for m in get("/markets", {"condition_ids": chunk, "limit": len(chunk) * 2, **extra}, base=GAMMA) or []:
            cid = m.get("conditionId")
            if cid and cid not in out:
                out[cid] = {
                    "prices": [float(x) for x in _json_list(m.get("outcomePrices"))],
                    "tokens": [str(x) for x in _json_list(m.get("clobTokenIds"))],
                    "closed": bool(m.get("closed")),
                }

    for i in range(0, len(ids), batch):
        chunk = ids[i:i + batch]
        pull(chunk, {})
        missing = [c for c in chunk if c not in out]
        if missing:  # some Gamma setups hide closed markets unless asked
            pull(missing, {"closed": "true"})
    return out


def final_price(row, markets):
    m = markets.get(row.get("conditionId"))
    if not m or not m["prices"]:
        return None
    idx = row.get("outcomeIndex")
    if idx is None and str(row.get("asset")) in m["tokens"]:
        idx = m["tokens"].index(str(row.get("asset")))
    try:
        return m["prices"][int(idx)], m["closed"]
    except (TypeError, ValueError, IndexError):
        return None


def copy_results(rows, markets, unit=10.0):
    """Simulate copying ONLY this trader's buys and sells.

    Converts, merges and splits are ignored. Sizing matches unit-based copying:
    their unit = their average buy in the window, and each of your bets is
    `unit` x (their bet / their unit). Because that scales every bet by the same
    factor, your ROI equals theirs on these trades.
    A sell only counts against shares bought in the window; selling shares
    that came from a convert (which you wouldn't own) is skipped.
    """
    trades = sorted((r for r in rows if r.get("type") == "TRADE"),
                    key=lambda r: int(r.get("timestamp") or 0))
    pos = {}  # asset -> running totals
    unpriced = 0
    for r in trades:
        price, size = float(r.get("price") or 0), float(r.get("size") or 0)
        if price <= 0 or size <= 0:
            continue
        p = pos.setdefault(r.get("asset"), {"row": r, "t_sh": 0.0, "t_cost": 0.0, "t_back": 0.0})
        if r.get("side") == "BUY":
            p["t_sh"] += size
            p["t_cost"] += size * price
        elif r.get("side") == "SELL" and p["t_sh"] > 0:
            sold = min(size, p["t_sh"])
            p["t_back"] += sold * price
            p["t_sh"] -= sold

    buy_sizes = [float(r.get("size") or 0) * float(r.get("price") or 0)
                 for r in trades if r.get("side") == "BUY"]
    buy_sizes = [b for b in buy_sizes if b > 0]
    res = {"buys": len(buy_sizes),
           "avg_bet": sum(buy_sizes) / len(buy_sizes) if buy_sizes else 0.0,
           "t_cost": 0.0, "t_pnl": 0.0,
           "resolved": 0, "resolved_wins": 0, "open": 0, "unpriced": 0}
    for p in pos.values():
        if p["t_cost"] == 0:
            continue
        fp = final_price(p["row"], markets)
        if fp is None:
            res["unpriced"] += 1
            continue
        price, closed = fp
        t_pnl = p["t_back"] + p["t_sh"] * price - p["t_cost"]
        res["t_cost"] += p["t_cost"]
        res["t_pnl"] += t_pnl
        if closed:
            res["resolved"] += 1
            res["resolved_wins"] += t_pnl > 0
        else:
            res["open"] += 1

    res["roi"] = round(100 * res["t_pnl"] / res["t_cost"], 1) if res["t_cost"] else None
    scale = unit / res["avg_bet"] if res["avg_bet"] else 0.0
    res["your_staked"] = res["t_cost"] * scale
    res["your_pnl"] = res["t_pnl"] * scale
    res["win_rate"] = round(100 * res["resolved_wins"] / res["resolved"], 1) if res["resolved"] else None
    return res


def check_wallet(wallet, days, cap, types=DEFAULT_TYPES, unit=10.0):
    """Debug one wallet: show the count and the date range it covers."""
    since = int(time.time()) - days * 86400
    n, rows = count_trades(wallet, since, cap, return_rows=True, types=types)
    fmt = lambda ts: time.strftime("%Y-%m-%d %H:%M", time.gmtime(int(ts)))
    print(f"Wallet {wallet}")
    print(f"Actions in last {days} days: {n if n < cap else str(cap) + '+'}")
    for t, c in type_breakdown(rows).items():
        print(f"  {t}: {c}")
    if rows:
        ts = [int(r.get("timestamp") or 0) for r in rows]
        print(f"Newest counted: {fmt(max(ts))} UTC")
        print(f"Oldest counted: {fmt(min(ts))} UTC")
        print(f"Window starts:  {fmt(since)} UTC")
        cids = [r.get("conditionId") for r in rows if r.get("type") == "TRADE"]
        c = copy_results(rows, fetch_markets(cids), unit)
        print(f"\nCopying only their buys/sells ({c['buys']} buys, converts/merges ignored):")
        print(f"  Their unit (average buy): ${c['avg_bet']:,.2f}  ->  your ${unit:g} unit")
        print(f"  Copy ROI: {c['roi']}%")
        print(f"  You'd have: P/L ${c['your_pnl']:,.2f} on ${c['your_staked']:,.2f} staked")
        print(f"  Resolved positions: {c['resolved']}  won: {c['win_rate']}%  still open: {c['open']}"
              + (f"  no price found: {c['unpriced']}" if c['unpriced'] else ""))


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
    p.add_argument("--types", default=DEFAULT_TYPES,
                   help="activity types to count, comma-separated (use TRADE for order-book trades only)")
    p.add_argument("--unit", type=float, default=10.0,
                   help="your unit; each copied bet = unit x (their bet / their average bet)")
    p.add_argument("--min-roi", type=float, default=None,
                   help="only keep traders whose copy ROI (%%) is at least this, e.g. 0 for profitable only")
    p.add_argument("--wallet", help="check a single wallet address instead of scanning the leaderboard")
    a = p.parse_args()

    if a.wallet:
        check_wallet(a.wallet, a.days, a.max_trades, a.types, a.unit)
        return

    since = int(time.time()) - a.days * 86400
    print(f"Pulling top {a.top} {a.category} traders by {a.order} ({a.period})...", file=sys.stderr)
    leaders = fetch_leaderboard(a.top, a.period, a.category, a.order)
    print(f"Got {len(leaders)}. Counting actions in last {a.days} days...", file=sys.stderr)

    keep, rows_by_wallet = [], {}
    for i, t in enumerate(leaders, 1):
        wallet = t["proxyWallet"]
        name = t.get("userName") or wallet[:10]
        try:
            n, rows = count_trades(wallet, since, a.max_trades, return_rows=True, types=a.types)
            kinds = type_breakdown(rows)
        except Exception as e:  # one bad wallet shouldn't kill the run
            print(f"  [{i}/{len(leaders)}] {name}: error {e}", file=sys.stderr)
            continue
        flag = "KEEP" if n < a.max_trades else f"{a.max_trades}+"
        print(f"  [{i}/{len(leaders)}] {name}: {n if n < a.max_trades else str(a.max_trades) + '+'} actions  {flag}",
              file=sys.stderr)
        if n < a.max_trades:
            rows_by_wallet[wallet] = rows
            keep.append({
                "rank": int(t.get("rank", 0) or 0),
                "username": t.get("userName"),
                "wallet": wallet,
                "actions_last_%dd" % a.days: n,
                "trades": kinds.get("TRADE", 0),
                "converts": kinds.get("CONVERSION", 0),
                "pnl": round(float(t.get("pnl") or 0), 2),
                "volume": round(float(t.get("vol") or 0), 2),
                "pnl_per_action": round(float(t.get("pnl") or 0) / n, 2) if n else None,
                "x_username": t.get("xUsername"),
                "profile": f"https://polymarket.com/profile/{wallet}",
            })
        time.sleep(a.sleep)

    print(f"Checking whether {len(keep)} traders' buys/sells were profitable...", file=sys.stderr)
    cids = [r.get("conditionId") for rows in rows_by_wallet.values() for r in rows if r.get("type") == "TRADE"]
    try:
        markets = fetch_markets(cids)
    except Exception as e:
        print(f"  couldn't load market prices: {e}", file=sys.stderr)
        markets = {}
    for r in keep:
        c = copy_results(rows_by_wallet[r["wallet"]], markets, a.unit)
        r.update({
            "buys": c["buys"],
            "their_unit": round(c["avg_bet"], 2),
            "copy_roi_pct": c["roi"],
            "your_pnl": round(c["your_pnl"], 2),
            "your_staked": round(c["your_staked"], 2),
            "win_rate_resolved_pct": c["win_rate"],
            "resolved_positions": c["resolved"],
            "open_positions": c["open"],
        })
    if a.min_roi is not None:
        keep = [r for r in keep if r["copy_roi_pct"] is not None and r["copy_roi_pct"] >= a.min_roi]

    keep.sort(key=lambda r: -(r["copy_roi_pct"] if r["copy_roi_pct"] is not None else -1e9))
    with open(f"{a.out}.json", "w") as f:
        json.dump(keep, f, indent=2)
    if keep:
        with open(f"{a.out}.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(keep[0].keys()))
            w.writeheader()
            w.writerows(keep)

    pct = lambda v: "-" if v is None else f"{v:+.1f}%"
    print(f"\n{len(keep)} traders made < {a.max_trades} actions in {a.days} days"
          + (f" with copy ROI >= {a.min_roi}%" if a.min_roi is not None else "") + ":\n")
    print(f"{'Rank':>4}  {'Trader':<20} {'Buys':>5} {'Their unit':>10} {'Copy ROI':>9} {'Your P/L':>9} {'Won':>6}")
    for r in keep:
        print(f"{r['rank']:>4}  {(r['username'] or r['wallet'][:12])[:20]:<20} "
              f"{r['buys']:>5} {'$' + format(r['their_unit'], ',.0f'):>10} "
              f"{pct(r['copy_roi_pct']):>9} {'$' + format(r['your_pnl'], ',.2f'):>9} "
              f"{pct(r['win_rate_resolved_pct']).lstrip('+'):>6}")
    print(f"\nSaved {a.out}.json and {a.out}.csv")


if __name__ == "__main__":
    main()
