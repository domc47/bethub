#!/usr/bin/env python3
"""
backfill_ledger.py  (run once)

Rebuilds the buys and sells the watcher alerted you about before it started
logging, by pulling each followed trader's public Polymarket activity, and
adds them to copy_ledger.csv. Same traders, same grouping, same min-bet rule
as copy_watcher.py.

  python backfill_ledger.py --since 2026-10-29

Only trades before the first row already in the ledger are added, so it is
safe to run after the watcher has started logging, and running it twice
does not create duplicates.

Limits:
  - "Price at alert" is left blank (the tracker uses their price instead).
    Log what you really paid in my_fills.csv to correct it.
  - Fills are grouped into 15-minute windows, the same as the watcher's runs,
    so counts can differ slightly from the alerts you got.
  - Uses each trader's current "their_unit" from low_trade_leaders.json.
  - "Sold X% of their position" is rebuilt from their trade history and
    current position, so it can be off when they held shares from before
    --since or already cashed out a settled market.
"""

import argparse
import csv
import time
from collections import defaultdict
from datetime import datetime, timezone

from copy_watcher import (LEDGER_FILE, append_ledger, group_fills, load_json,
                          market_link, new_trades, remaining_shares, ledger_row)

WINDOW = 15 * 60


def existing(path):
    try:
        with open(path, newline="") as f:
            rows = list(csv.DictReader(f))
    except FileNotFoundError:
        return None, set()
    first = min((int(r["timestamp"]) for r in rows if r.get("timestamp")), default=None)
    keys = {(r["trader"], r["asset"], r["side"], int(r["timestamp"]) // WINDOW) for r in rows}
    return first, keys


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--since", required=True, help="start date, YYYY-MM-DD (UTC)")
    p.add_argument("--traders", default="low_trade_leaders.json")
    p.add_argument("--unit", type=float, default=10.0)
    p.add_argument("--min-bet", type=float, default=1.0)
    a = p.parse_args()

    since = int(datetime.strptime(a.since, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
    first_logged, have = existing(LEDGER_FILE)
    until = first_logged or int(time.time())
    traders = load_json(a.traders, [])
    out = []

    for t in traders:
        wallet = t["wallet"]
        name = t.get("username") or wallet[:10]
        try:
            rows = new_trades(wallet, since, {})
        except Exception as e:
            print(f"{name}: error {e}")
            continue

        # same grouping as the watcher: one order per outcome+side per 15-min run
        buckets = defaultdict(list)
        for r in rows:
            buckets[int(r.get("timestamp") or 0) // WINDOW].append(r)
        groups = [g for b in sorted(buckets) for g in group_fills(buckets[b])]
        logged = [g for g in groups if g["last"] < until]

        # rebuild how much of their position each sell was
        by_asset = defaultdict(list)
        for g in groups:
            by_asset[g["row"].get("asset")].append(g)
        sell_pct = {}
        for asset, gs in by_asset.items():
            gs.sort(key=lambda g: g["last"])
            sells = [g for g in gs if g["row"].get("side") == "SELL"]
            if not sells:
                continue
            now_held = remaining_shares(wallet, gs[0]["row"].get("conditionId"), asset) or 0.0
            held_fwd, after = 0.0, now_held
            fwd = {}
            for g in gs:  # forward: shares bought in the window so far
                if g["row"].get("side") == "BUY":
                    held_fwd += g["shares"]
                else:
                    fwd[id(g)] = held_fwd
                    held_fwd = max(held_fwd - g["shares"], 0.0)
            for g in reversed(gs):  # backward: from their position today
                if g["row"].get("side") == "SELL":
                    before = max(after + g["shares"], fwd[id(g)])
                    sell_pct[id(g)] = min(g["shares"] / before, 1.0) if before > 0 else None
                    after = before
                else:
                    after = max(after - g["shares"], 0.0)
            time.sleep(0.2)

        their_unit = float(t.get("their_unit") or 0)
        added = 0
        for g in logged:
            r = g["row"]
            if r.get("side") == "BUY" and their_unit:
                if a.unit * g["usd"] / their_unit < a.min_bet:
                    continue
            row = ledger_row(t, g, wallet, market_link(r, t), sell_pct.get(id(g)), None)
            k = (row["trader"], str(row["asset"]), row["side"], int(row["timestamp"]) // WINDOW)
            if k in have:
                continue
            have.add(k)
            out.append(row)
            added += 1
        print(f"{name}: {added} bets")
        time.sleep(0.2)

    out.sort(key=lambda r: r["timestamp"])
    append_ledger(out)
    print(f"Added {len(out)} past bets to {LEDGER_FILE}.")


if __name__ == "__main__":
    main()
