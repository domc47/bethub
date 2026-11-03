#!/usr/bin/env python3
"""
copy_watcher.py

Every run (GitHub Actions, every 15 min): check each trader in
low_trade_leaders.json for new buys/sells since the last run and send an ntfy
notification with the market, price, their bet, and that bet converted to
your unit.

Every alert is also logged as a row in copy_ledger.csv, which build_ledger.py
turns into copy_tracker.xlsx. Each alert title starts with its bet number
(#12), which is the row number in the ledger, so you can log your real fill
in my_fills.csv as: 12,0.47,9.80

  your bet = your unit x (their bet / their unit)
  their unit = their average buy from the last scan ("their_unit")

Converts, merges and splits are ignored. Fills from the same order are
combined into one alert.

Env vars:
  NTFY_TOPIC   (required) your ntfy topic
  NTFY_SERVER  (optional) default https://ntfy.sh
  NTFY_TOKEN   (optional) access token if your topic is protected
  ALERT_TZ     (optional) time zone for alert times, default America/Chicago
"""

import argparse
import csv
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone

from low_trade_leaders import get, fetch_markets

STATE_FILE = "watcher_state.json"
LEDGER_FILE = "copy_ledger.csv"
LEDGER_FIELDS = ["timestamp", "time", "trader", "market", "outcome", "side", "price",
                 "alert_price", "their_usd", "their_unit", "their_sell_pct",
                 "condition_id", "asset", "link"]


def _tz():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(os.environ.get("ALERT_TZ", "America/Chicago"))
    except Exception:
        return timezone.utc


def local_time(ts):
    return datetime.fromtimestamp(ts, _tz()).strftime("%-I:%M %p %Z")


def local_datetime(ts):
    return datetime.fromtimestamp(ts, _tz()).strftime("%Y-%m-%d %-I:%M %p %Z")


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return default


def new_trades(wallet, since_ts, seen):
    rows, offset = [], 0
    while True:
        batch = get("/activity", {
            "user": wallet, "type": "TRADE", "start": since_ts,
            "limit": 500, "offset": offset,
            "sortBy": "TIMESTAMP", "sortDirection": "DESC",
        }) or []
        rows += [r for r in batch if int(r.get("timestamp") or 0) >= since_ts]
        if len(batch) < 500 or offset >= 4500:
            break
        offset += 500
    out = []
    for r in rows:
        key = f'{r.get("transactionHash")}:{r.get("asset")}:{r.get("side")}:{r.get("size")}'
        if key not in seen:
            r["_key"] = key
            out.append(r)
    return out


def group_fills(rows):
    """Combine fills of the same market/outcome/side into one order."""
    groups = {}
    for r in rows:
        k = (r.get("asset"), r.get("side"))
        g = groups.setdefault(k, {"row": r, "shares": 0.0, "usd": 0.0, "fills": 0,
                                  "first": int(r.get("timestamp") or 0), "last": 0})
        size, price = float(r.get("size") or 0), float(r.get("price") or 0)
        g["shares"] += size
        g["usd"] += size * price
        g["fills"] += 1
        g["first"] = min(g["first"], int(r.get("timestamp") or 0))
        g["last"] = max(g["last"], int(r.get("timestamp") or 0))
    for g in groups.values():
        g["price"] = g["usd"] / g["shares"] if g["shares"] else 0
    return sorted(groups.values(), key=lambda g: g["last"])


def remaining_shares(wallet, condition_id, asset):
    try:
        for p in get("/positions", {"user": wallet, "market": condition_id, "sizeThreshold": 0}) or []:
            if str(p.get("asset")) == str(asset):
                return float(p.get("size") or 0)
        return 0.0
    except Exception:
        return None


def notify(server, topic, token, title, message, tags, click):
    body = {"topic": topic, "title": title, "message": message, "tags": tags}
    if click:
        body["click"] = click
    req = urllib.request.Request(server.rstrip("/"), data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    urllib.request.urlopen(req, timeout=20).read()


def market_link(r, trader):
    slug = r.get("eventSlug") or r.get("slug")
    return f"https://polymarket.com/event/{slug}" if slug else trader.get("profile")


def build_alert(trader, g, unit, wallet):
    """Returns title, message, tags, click link, your bet, and the share of their
    position they sold (0-1, sells only; None if unknown or a buy)."""
    r = g["row"]
    name = trader.get("username") or wallet[:10]
    their_unit = float(trader.get("their_unit") or 0)
    market = r.get("title") or "Unknown market"
    outcome = r.get("outcome") or f'outcome #{r.get("outcomeIndex")}'
    cents = g["price"] * 100
    units = g["usd"] / their_unit if their_unit else 0
    your_bet = unit * units
    click = market_link(r, trader)
    when = local_time(g["last"])
    fills = f' ({g["fills"]} fills)' if g["fills"] > 1 else ""

    if r.get("side") == "BUY":
        title = f"BUY - {name}"
        msg = (f"{market}\n"
               f"{outcome} @ {cents:.1f}c\n"
               f"They bet ${g['usd']:,.2f}{fills} = {units:.2f}u (their unit ${their_unit:,.0f})\n"
               f"YOUR BET: ${your_bet:,.2f} ({your_bet / g['price']:,.1f} shares)\n"
               f"{when}")
        return title, msg, ["green_circle"], click, your_bet, None

    sell_pct = None
    left = remaining_shares(wallet, r.get("conditionId"), r.get("asset"))
    if left is not None and g["shares"] + left > 0:
        sell_pct = g["shares"] / (g["shares"] + left)
        pct = 100 * sell_pct
        pos_line = (f"Sold {pct:.0f}% of their position"
                    + (" (fully out)" if left < 0.01 else "") + "\n"
                    f"YOUR MOVE: sell {pct:.0f}% of your shares in this outcome\n")
    else:
        pos_line = "YOUR MOVE: sell the same share of your position\n"
    title = f"SELL - {name}"
    msg = (f"{market}\n"
           f"{outcome} @ {cents:.1f}c\n"
           f"They sold ${g['usd']:,.2f}{fills} = {units:.2f}u (their unit ${their_unit:,.0f})\n"
           f"{pos_line}"
           f"At your unit that's ~${your_bet:,.2f}\n"
           f"{when}")
    return title, msg, ["red_circle"], click, your_bet, sell_pct


# ---------- ledger ----------

def ledger_count(path=LEDGER_FILE):
    try:
        with open(path, newline="") as f:
            return sum(1 for _ in csv.DictReader(f))
    except FileNotFoundError:
        return 0


def current_price(condition_id, asset):
    """Market price of this outcome right now (used as 'price at alert')."""
    try:
        m = fetch_markets([condition_id]).get(condition_id)
        if m and asset in m["tokens"]:
            return m["prices"][m["tokens"].index(asset)]
    except Exception:
        pass
    return None


def ledger_row(trader, g, wallet, link, sell_pct, alert_price):
    r = g["row"]
    return {
        "timestamp": g["last"],
        "time": local_datetime(g["last"]),
        "trader": trader.get("username") or wallet[:10],
        "market": r.get("title") or "Unknown market",
        "outcome": r.get("outcome") or f'outcome #{r.get("outcomeIndex")}',
        "side": r.get("side"),
        "price": round(g["price"], 4),
        "alert_price": "" if alert_price is None else round(float(alert_price), 4),
        "their_usd": round(g["usd"], 2),
        "their_unit": trader.get("their_unit") or 0,
        "their_sell_pct": "" if sell_pct is None else round(sell_pct, 4),
        "condition_id": r.get("conditionId"),
        "asset": r.get("asset"),
        "link": link or "",
    }


def append_ledger(rows, path=LEDGER_FILE):
    if not rows:
        return
    new_file = not os.path.exists(path) or os.path.getsize(path) == 0
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=LEDGER_FIELDS)
        if new_file:
            w.writeheader()
        w.writerows(rows)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--traders", default="low_trade_leaders.json", help="output of low_trade_leaders.py")
    p.add_argument("--unit", type=float, default=10.0, help="your unit in dollars")
    p.add_argument("--min-bet", type=float, default=1.0,
                   help="skip buy alerts where your converted bet would be under this many dollars")
    p.add_argument("--lookback", type=int, default=20,
                   help="minutes to look back on the very first run")
    p.add_argument("--dry-run", action="store_true", help="print alerts instead of sending them")
    a = p.parse_args()

    topic = os.environ.get("NTFY_TOPIC")
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh")
    token = os.environ.get("NTFY_TOKEN")
    if not topic and not a.dry_run:
        sys.exit("Set NTFY_TOPIC (or use --dry-run).")

    traders = load_json(a.traders, [])
    if not traders:
        sys.exit(f"No traders in {a.traders}. Run low_trade_leaders.py first.")
    state = load_json(STATE_FILE, {"last": {}, "seen": {}})
    now = int(time.time())
    sent = 0
    bet_no = ledger_count()
    new_rows = []

    for t in traders:
        wallet = t["wallet"]
        since = int(state["last"].get(wallet, now - a.lookback * 60))
        seen = state["seen"].get(wallet, {})
        try:
            rows = new_trades(wallet, since, seen)
        except Exception as e:
            print(f"{t.get('username') or wallet}: error {e}", file=sys.stderr)
            continue

        for g in group_fills(rows):
            title, msg, tags, click, your_bet, sell_pct = build_alert(t, g, a.unit, wallet)
            if g["row"].get("side") == "BUY" and your_bet < a.min_bet:
                print(f"skip (your bet ${your_bet:.2f} < ${a.min_bet:g}): {title}", file=sys.stderr)
                continue
            bet_no += 1
            title = f"#{bet_no} {title}"
            if a.dry_run:
                print(f"--- {title}\n{msg}\n")
            else:
                try:
                    notify(server, topic, token, title, msg, tags, click)
                except Exception as e:
                    print(f"ntfy error: {e}", file=sys.stderr)
            alert_price = current_price(g["row"].get("conditionId"), g["row"].get("asset"))
            new_rows.append(ledger_row(t, g, wallet, click, sell_pct, alert_price))
            sent += 1

        for r in rows:
            seen[r["_key"]] = int(r.get("timestamp") or 0)
        newest = max([since] + [int(r.get("timestamp") or 0) for r in rows])
        # keep 2h of seen keys so same-second trades on the boundary aren't re-sent
        state["seen"][wallet] = {k: v for k, v in seen.items() if v >= newest - 7200}
        state["last"][wallet] = newest
        time.sleep(0.2)

    if not a.dry_run:
        append_ledger(new_rows)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)
    print(f"Checked {len(traders)} traders, sent {sent} alerts, logged {len(new_rows)}.",
          file=sys.stderr)


if __name__ == "__main__":
    main()
