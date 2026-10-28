#!/usr/bin/env python3
"""
whale_watch.py - Polymarket "big bet" alerts for the top-10 leaderboard, sent via ntfy.

  1. Alerts when a top-N trader places a bet bigger than their own average bet.
  2. Stays quiet if they are on both sides of that market.
  3. Sends a follow-up if they later hedge, sell out, or merge that position.

Does one pass per run (built for a GitHub Actions cron). State lives in whale_state.json.
Standard library only - no pip installs.
"""
import json
import os
import statistics
import time
import urllib.parse
import urllib.request

import tracker  # paper-trading ledger (tracker.py)


def env(name, default, cast=str):
    v = os.environ.get(name, "")
    return cast(v) if v.strip() else default


# ---- Where alerts go ----
NTFY_SERVER = env("NTFY_SERVER", "https://ntfy.sh")
NTFY_TOPIC = env("NTFY_TOPIC", "")
NTFY_TOKEN = env("NTFY_TOKEN", "")             # only if your topic is access-protected
STATE_FILE = env("STATE_FILE", "whale_state.json")

# ---- Who to watch ----
WATCHLIST_FILE = env("WATCHLIST_FILE", "watchlist.json")  # from screener.py; falls back to leaderboard
TOP_N = env("TOP_N", 10, int)
LB_PERIOD = env("LB_PERIOD", "MONTH")          # DAY | WEEK | MONTH | ALL
LB_ORDER = env("LB_ORDER", "PNL")              # PNL | VOL
LB_CATEGORY = env("LB_CATEGORY", "OVERALL")    # OVERALL | POLITICS | SPORTS | CRYPTO | ...
EXTRA_WALLETS = [w.strip().lower() for w in env("EXTRA_WALLETS", "").split(",") if w.strip()]

# ---- What counts as a big bet ----
AVG_METHOD = env("AVG_METHOD", "mean")         # mean | median (median ignores one-off monster bets)
AVG_MULTIPLIER = env("AVG_MULTIPLIER", 1.0, float)   # 1.0 = above average, 2.0 = double their average
MIN_BET_USD = env("MIN_BET_USD", 100, float)   # floor so traders with tiny averages don't spam you
MIN_HISTORY_BETS = env("MIN_HISTORY_BETS", 10, int)  # need this many past bets to trust the average
MAX_ENTRY_PRICE = env("MAX_ENTRY_PRICE", 0.97, float)  # skip 98-99c "parking cash" bets
GROUP_WINDOW_MIN = env("GROUP_WINDOW_MIN", 15, int)  # fills this close together = one bet
STALE_MIN = env("STALE_MIN", 180, int)         # never alert on a bet older than this

# ---- Your sizing and price protection ----
UNIT_USD = env("UNIT_USD", 10, float)          # your 1-unit stake; alerts scale it by their bet / their avg
MAX_UNITS = env("MAX_UNITS", 0, float)         # cap on units per alert (0 = no cap)
MAX_PRICE_MOVE_PCT = env("MAX_PRICE_MOVE_PCT", 3.0, float)  # skip if current price is >3% worse than theirs

# ---- Both-sides and hedge logic ----
BOTH_SIDES_WINDOW_MIN = env("BOTH_SIDES_WINDOW_MIN", 60, int)  # other side bought this close = both sides
BOTH_SIDES_RATIO = env("BOTH_SIDES_RATIO", 0.25, float)        # ...if it's at least 25% of the bet
CONFIRM_DELAY_MIN = env("CONFIRM_DELAY_MIN", 0, int)           # hold alerts this long to catch late other-side buys
HEDGE_WATCH_DAYS = env("HEDGE_WATCH_DAYS", 30, int)            # how long to watch an alerted bet for hedges
HEDGE_MIN_USD = env("HEDGE_MIN_USD", 50, float)                # ignore hedges/sales smaller than this
RELATED_MARKET_ALERTS = env("RELATED_MARKET_ALERTS", "1") == "1"  # flag buys in other markets of the same event

API = "https://data-api.polymarket.com"
CLOB = "https://clob.polymarket.com"


# ------------------------------------------------------------------ API helpers
def get(path, base=API, **params):
    url = f"{base}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "bethub-whale-watch/1.0"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.load(r)
        except Exception:
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))


def top_traders():
    """Screened watchlist if there is one, otherwise the raw leaderboard top N."""
    try:
        with open(WATCHLIST_FILE) as f:
            wl = json.load(f).get("wallets", [])
        if wl:
            return {w["wallet"].lower(): {"rank": str(i), "name": w["name"], "stat": w.get("stat", "")}
                    for i, w in enumerate(wl, 1)}
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    rows = get("/v1/leaderboard", category=LB_CATEGORY, timePeriod=LB_PERIOD,
               orderBy=LB_ORDER, limit=TOP_N)
    return {r["proxyWallet"].lower(): {"rank": str(r.get("rank", "?")),
                                       "name": r.get("userName") or r["proxyWallet"][:10]}
            for r in rows}


def activity(wallet):
    rows = get("/activity", user=wallet, limit=500, sortBy="TIMESTAMP", sortDirection="DESC")
    return sorted(rows, key=lambda r: r["timestamp"])


def opposite_position_usd(wallet, condition_id, outcome_index):
    """Cost basis the wallet currently holds on the OTHER outcome of this market."""
    try:
        rows = get("/positions", user=wallet, market=condition_id)
    except Exception as e:
        print(f"  positions lookup failed ({e}); relying on recent trades only")
        return 0.0
    return sum(float(p.get("initialValue") or 0) for p in rows
               if p.get("conditionId") == condition_id
               and int(p.get("outcomeIndex", -1)) != outcome_index)


def best_ask(token_id):
    """Cheapest price you could buy this outcome at right now (None if unavailable)."""
    try:
        book = get("/book", base=CLOB, token_id=token_id)
        asks = [float(a["price"]) for a in book.get("asks", [])]
        return min(asks) if asks else None
    except Exception as e:
        print(f"  order book lookup failed ({e})")
        return None


def notify(title, message, tags, priority=3, click=None):
    print(f"NOTIFY | {title} | {message}".replace("\n", " / "))
    if not NTFY_TOPIC:
        return
    body = {"topic": NTFY_TOPIC, "title": title, "message": message,
            "tags": tags, "priority": priority}
    if click:
        body["click"] = click
    headers = {"Content-Type": "application/json"}
    if NTFY_TOKEN:
        headers["Authorization"] = f"Bearer {NTFY_TOKEN}"
    req = urllib.request.Request(NTFY_SERVER, data=json.dumps(body).encode(),
                                 headers=headers, method="POST")
    try:
        urllib.request.urlopen(req, timeout=15)
    except Exception as e:
        print(f"  ntfy send failed: {e}")


# ------------------------------------------------------------------ bet logic
def is_trade(r):
    return r.get("type") == "TRADE" and not r.get("isCombo")


def usd(r):
    return float(r.get("usdcSize") or float(r["size"]) * float(r["price"]))


def cents(p):
    return f"{p * 100:.1f}".rstrip("0").rstrip(".") + "\u00a2"


def link(slug):
    return f"https://polymarket.com/event/{slug}" if slug else None


def group_bets(wallet, rows):
    """Big orders fill in many pieces. Merge same-outcome, same-side fills that land
    within GROUP_WINDOW_MIN of each other into one 'bet' so sizes compare fairly."""
    bets, open_groups = [], {}
    for r in rows:
        if not is_trade(r):
            continue
        key = (r["asset"], r["side"])
        g = open_groups.get(key)
        if g and r["timestamp"] - g["last_ts"] <= GROUP_WINDOW_MIN * 60:
            g["usd"] += usd(r)
            g["shares"] += float(r["size"])
            g["last_ts"] = r["timestamp"]
        else:
            g = {"id": f"{wallet}:{r['asset']}:{r['side']}:{r['timestamp']}",
                 "wallet": wallet, "asset": r["asset"], "side": r["side"],
                 "conditionId": r["conditionId"], "outcomeIndex": int(r["outcomeIndex"]),
                 "outcome": r.get("outcome", "?"), "title": r.get("title", "?"),
                 "eventSlug": r.get("eventSlug", ""), "usd": usd(r),
                 "shares": float(r["size"]), "first_ts": r["timestamp"], "last_ts": r["timestamp"]}
            open_groups[key] = g
            bets.append(g)
    for g in bets:
        g["price"] = g["usd"] / g["shares"] if g["shares"] else 0.0
    return bets


def find_big_bets(wallet, who, bets, cursor, state, now):
    history = [b["usd"] for b in bets if b["side"] == "BUY" and b["last_ts"] <= cursor]
    if len(history) < MIN_HISTORY_BETS:
        print(f"  {who['name']}: only {len(history)} past bets, average not reliable yet")
        return
    avg = statistics.median(history) if AVG_METHOD == "median" else statistics.fmean(history)
    for b in bets:
        if b["side"] != "BUY" or b["last_ts"] <= cursor or b["id"] in state["handled"]:
            continue
        if b["usd"] <= avg * AVG_MULTIPLIER or b["usd"] < MIN_BET_USD:
            continue  # not big (yet) - if more fills arrive it gets re-checked next run
        state["handled"][b["id"]] = now
        if b["price"] > MAX_ENTRY_PRICE:
            print(f"  skip near-certain bet at {cents(b['price'])}: {b['title']}")
            continue
        if now - b["last_ts"] > STALE_MIN * 60:
            print(f"  skip stale bet: {b['title']}")
            continue
        state["pending"].append(dict(b, name=who["name"], rank=who["rank"], avg=avg, detected=now,
                                       stat=who.get("stat", "")))


def process_pending(wallet, rows, bets, state, now):
    by_id = {b["id"]: b for b in bets}
    win = BOTH_SIDES_WINDOW_MIN * 60
    remaining = []
    for p in state["pending"]:
        if p["wallet"] != wallet or now - p["detected"] < CONFIRM_DELAY_MIN * 60:
            remaining.append(p)
            continue
        if p["id"] in by_id:  # the bet may have kept filling since we first saw it
            fresh = by_id[p["id"]]
            p.update({k: fresh[k] for k in ("usd", "shares", "price", "last_ts")})

        other_recent = sum(usd(r) for r in rows
                           if is_trade(r) and r["side"] == "BUY"
                           and r["conditionId"] == p["conditionId"]
                           and int(r["outcomeIndex"]) != p["outcomeIndex"]
                           and p["first_ts"] - win <= r["timestamp"] <= p["last_ts"] + win)
        other = max(other_recent,
                    opposite_position_usd(wallet, p["conditionId"], p["outcomeIndex"]))
        if other >= BOTH_SIDES_RATIO * p["usd"]:
            print(f"  suppressed (both sides): {p['name']} ${p['usd']:,.0f} {p['outcome']} "
                  f"vs ${other:,.0f} on the other side - {p['title']}")
            continue

        # Price check: is the price still within MAX_PRICE_MOVE_PCT of what they paid?
        now_price = best_ask(p["asset"])
        if now_price is None:
            if now - p["last_ts"] <= STALE_MIN * 60:
                remaining.append(p)  # couldn't price it - retry next run
            continue
        move = (now_price - p["price"]) / p["price"]
        if move * 100 > MAX_PRICE_MOVE_PCT:
            print(f"  skipped (price moved {move:+.1%}): {p['name']} paid {cents(p['price'])}, "
                  f"now {cents(now_price)} - {p['title']}")
            continue

        # Convert to your units: 3x their average bet = 3 units of yours.
        units = p["usd"] / p["avg"]
        if MAX_UNITS:
            units = min(units, MAX_UNITS)
        p["stake"] = max(UNIT_USD, round(UNIT_USD * units))
        _, fee, _ = tracker.buy_with_fee(p["stake"], now_price, p["conditionId"])

        notify(f"#{p['rank']} {p['name']}: {p['outcome']} - bet ${p['stake']:,.0f}",
               f"{p['title']}\n"
               f"They bet ${p['usd']:,.0f} ({p['usd'] / p['avg']:.1f}x their ${p['avg']:,.0f} avg)\n"
               f"They paid {cents(p['price'])}, now {cents(now_price)} ({move:+.1%})\n"
               f"Your stake: ${p['stake']:,.0f} ({p['stake'] / UNIT_USD:g} units), "
               f"fee ~${fee:,.2f} ({fee / p['stake']:.1%})"
               + (f"\n{p['stat']}" if p.get("stat") else ""),
               ["whale"], priority=4, click=link(p["eventSlug"]))
        add_watch(p, state)
        tracker.record_entry(p, now_price, now)
    state["pending"] = remaining


def add_watch(p, state):
    for w in state["watch"]:
        if w["wallet"] == p["wallet"] and w["asset"] == p["asset"]:
            w["usd"] += p["usd"]
            w["shares"] += p["shares"]
            w["last_ts"] = max(w["last_ts"], p["last_ts"])
            w["stake"] = w.get("stake", 0) + p["stake"]
            return
    state["watch"].append({k: p[k] for k in (
        "wallet", "name", "asset", "conditionId", "outcomeIndex", "outcome", "title",
        "eventSlug", "usd", "shares", "price", "first_ts", "last_ts", "stake")} | {"sold_shares": 0.0})


def check_followups(wallet, new_rows, state, now):
    keep = []
    for w in state["watch"]:
        if w["wallet"] != wallet:
            keep.append(w)
            continue
        if now - w["first_ts"] > HEDGE_WATCH_DAYS * 86400:
            continue  # stop watching old bets

        hedged, related = {}, {}
        sold_sh = sold_usd = merged = 0.0
        resolved = False
        for r in new_rows:
            if r["timestamp"] <= w["last_ts"]:
                continue
            same_mkt = r.get("conditionId") == w["conditionId"]
            t = r.get("type")
            if same_mkt and t == "REDEEM":
                resolved = True
            elif same_mkt and t == "MERGE":
                merged += usd(r)
            elif is_trade(r):
                if same_mkt and r["side"] == "BUY" and int(r["outcomeIndex"]) != w["outcomeIndex"]:
                    o = r.get("outcome", "other side")
                    hedged[o] = hedged.get(o, 0.0) + usd(r)
                elif r["asset"] == w["asset"] and r["side"] == "SELL":
                    sold_sh += float(r["size"])
                    sold_usd += usd(r)
                elif (RELATED_MARKET_ALERTS and not same_mkt and r["side"] == "BUY"
                      and w["eventSlug"] and r.get("eventSlug") == w["eventSlug"]):
                    k = f"{r.get('title', '?')} -> {r.get('outcome', '?')}"
                    related[k] = related.get(k, 0.0) + usd(r)

        url = link(w["eventSlug"])
        head = (f"{w['title']}\nOriginal: ${w['usd']:,.0f} on {w['outcome']} @ {cents(w['price'])}"
                f" (your copy: ${w.get('stake', 0):,.0f})")
        for outcome, amt in hedged.items():
            if amt >= HEDGE_MIN_USD:
                notify(f"HEDGE: {w['name']} bought {outcome}",
                       f"{head}\nNow: ${amt:,.0f} on {outcome} ({amt / w['usd']:.0%} of original)",
                       ["warning"], priority=4, click=url)
        if sold_usd >= HEDGE_MIN_USD:
            w["sold_shares"] += sold_sh
            frac = min(1.0, w["sold_shares"] / w["shares"]) if w["shares"] else 1.0
            verb = "EXITED" if frac >= 0.95 else f"SOLD {frac:.0%} of"
            tracker.record_exit(wallet, w["asset"], 1.0 if frac >= 0.95 else frac, now)
            notify(f"{verb}: {w['name']} on {w['outcome']}",
                   f"{head}\nSold ${sold_usd:,.0f} @ {cents(sold_usd / sold_sh)}",
                   ["chart_with_downwards_trend"], priority=4, click=url)
        if merged >= HEDGE_MIN_USD:
            notify(f"UNWOUND: {w['name']} merged {w['outcome']} position",
                   f"{head}\nMerged ${merged:,.0f} of Yes+No back to cash",
                   ["warning"], priority=4, click=url)
        for desc, amt in related.items():
            if amt >= HEDGE_MIN_USD:
                notify(f"Possible hedge: {w['name']}",
                       f"{head}\nAlso bought ${amt:,.0f} in a related market:\n{desc}",
                       ["eyes"], priority=3, click=url)

        if resolved or (w["shares"] and w["sold_shares"] >= 0.95 * w["shares"]):
            continue  # done with this one
        keep.append(w)
    state["watch"] = keep


# ------------------------------------------------------------------ main
def load_state():
    try:
        with open(STATE_FILE) as f:
            s = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        s = {}
    for k, default in (("cursor", {}), ("handled", {}), ("pending", []), ("watch", [])):
        s.setdefault(k, default)
    return s


def main():
    now = int(time.time())
    state = load_state()
    try:
        top = top_traders()
    except Exception as e:
        print(f"Leaderboard fetch failed: {e}")
        return
    for w in EXTRA_WALLETS:
        top.setdefault(w, {"rank": "+", "name": w[:10]})

    # Keep polling wallets that fell off the leaderboard while we still watch their bets.
    wallets = set(top) | {w["wallet"] for w in state["watch"]} | {p["wallet"] for p in state["pending"]}
    new_cursor = {}
    for wallet in sorted(wallets):
        cursor = state["cursor"].get(wallet)
        try:
            rows = activity(wallet)
        except Exception as e:
            print(f"{wallet}: fetch failed ({e})")
            if cursor is not None:
                new_cursor[wallet] = cursor
            continue
        latest = max((r["timestamp"] for r in rows), default=now)
        if cursor is None:
            # First time seeing this wallet: record a baseline, don't alert on old trades.
            new_cursor[wallet] = latest
            print(f"{wallet}: baseline set")
            continue

        new_rows = [r for r in rows if r["timestamp"] > cursor]
        bets = group_bets(wallet, rows)
        if wallet in top:
            find_big_bets(wallet, top[wallet], bets, cursor, state, now)
        process_pending(wallet, rows, bets, state, now)
        check_followups(wallet, new_rows, state, now)
        new_cursor[wallet] = max(cursor, latest)

    tracker.settle(state, now)
    tracker.save()
    state["cursor"] = new_cursor  # wallets no longer tracked are dropped, so no stale catch-up later
    state["handled"] = {k: t for k, t in state["handled"].items() if now - t < 14 * 86400}
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=1)


if __name__ == "__main__":
    main()
