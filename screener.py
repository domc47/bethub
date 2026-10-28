#!/usr/bin/env python3
"""
screener.py - weekly: find leaderboard traders worth copying.

Pulls candidates from several Polymarket leaderboards, throws out wallets whose profits come
from something other than picking winners (market making, arbitrage/merging, reward farming,
bots, 90c+ "sure thing" grinding, speed markets), then checks that the survivors' BIG bets
actually make money and that the market moves their way after they bet.

Writes watchlist.json. whale_watch.py follows that list instead of the raw top 10.
"""
import json
import statistics
import time
from collections import Counter, defaultdict

from whale_watch import (CLOB, EXTRA_WALLETS, env, get, group_bets, is_trade, notify, usd)

LEADERBOARDS = env("LEADERBOARDS", "OVERALL:MONTH,OVERALL:ALL,SPORTS:MONTH,POLITICS:MONTH")
CANDIDATES_PER_BOARD = env("CANDIDATES_PER_BOARD", 25, int)
LOOKBACK_DAYS = env("LOOKBACK_DAYS", 90, int)
WATCHLIST_FILE = env("WATCHLIST_FILE", "watchlist.json")
MAX_WATCH = env("MAX_WATCH", 15, int)

# ---- Fingerprint limits: exceed any one = not a directional bettor ----
MAX_FILLS_PER_DAY = env("MAX_FILLS_PER_DAY", 150, float)   # bots
MAX_TWO_SIDED = env("MAX_TWO_SIDED", 0.25, float)         # share of markets bought on both sides
MAX_QUICK_FLIP = env("MAX_QUICK_FLIP", 0.40, float)       # share of positions sold within QUICK_FLIP_HOURS
QUICK_FLIP_HOURS = env("QUICK_FLIP_HOURS", 6, float)
MAX_HIGH_PRICE_SHARE = env("MAX_HIGH_PRICE_SHARE", 0.50, float)  # share of $ bought at 90c+
MAX_MERGE_RATE = env("MAX_MERGE_RATE", 0.10, float)       # share of markets merged back to cash
MAX_REWARD_SHARE = env("MAX_REWARD_SHARE", 0.25, float)   # liquidity rewards vs. trading profit
MAX_SPEED_SHARE = env("MAX_SPEED_SHARE", 0.30, float)     # share of $ in short-term up/down markets
SPEED_PATTERNS = ("up or down", "updown", "up-or-down")

# ---- Edge requirements ----
MIN_CLOSED = env("MIN_CLOSED", 20, int)        # closed positions needed to judge anyone
MIN_BIG = env("MIN_BIG", 8, int)               # closed above-average positions needed
MAX_TOP_WIN_SHARE = env("MAX_TOP_WIN_SHARE", 0.5, float)  # best single win can't be over half their profit
MIN_PROFITABLE_MONTHS = env("MIN_PROFITABLE_MONTHS", 2, int)  # of the 30-day blocks in LOOKBACK_DAYS
CLV_HOURS = env("CLV_HOURS", 24, int)          # how far after the bet to check the price
CLV_SAMPLES = env("CLV_SAMPLES", 25, int)
MIN_CLV_SAMPLES = env("MIN_CLV_SAMPLES", 5, int)


def candidates():
    out = {}
    for spec in LEADERBOARDS.split(","):
        cat, period = (x.strip().upper() for x in spec.split(":"))
        try:
            rows = get("/v1/leaderboard", category=cat, timePeriod=period,
                       orderBy="PNL", limit=CANDIDATES_PER_BOARD)
        except Exception as e:
            print(f"leaderboard {spec} failed: {e}")
            continue
        for r in rows:
            w = r["proxyWallet"].lower()
            c = out.setdefault(w, {"name": r.get("userName") or w[:10], "boards": []})
            c["boards"].append(f"{cat.title()} {period.lower()} #{r.get('rank')}")
    for w in EXTRA_WALLETS:
        out.setdefault(w, {"name": w[:10], "boards": ["manual"]})
    return out


def paged(path, page_size, max_pages, since, **params):
    """Newest-first pages until we pass `since`. Also reports if we ran out of pages first."""
    rows, capped = [], False
    for i in range(max_pages):
        batch = get(path, limit=page_size, offset=i * page_size, **params)
        rows += batch
        if len(batch) < page_size or min(r["timestamp"] for r in batch) < since:
            break
        capped = i == max_pages - 1
        time.sleep(0.15)
    return [r for r in rows if r["timestamp"] >= since], capped


def fingerprint(rows, now):
    trades = [r for r in rows if is_trade(r)]
    buys = [r for r in trades if r["side"] == "BUY"]
    reasons = []
    if not buys:
        return {}, ["no recent bets"]

    span_days = max(1.0, (now - min(r["timestamp"] for r in rows)) / 86400)
    fills_per_day = len(trades) / span_days

    sides = defaultdict(lambda: defaultdict(float))
    for r in buys:
        sides[r["conditionId"]][int(r["outcomeIndex"])] += usd(r)
    markets = len(sides)
    two_sided = sum(1 for s in sides.values()
                    if len(s) >= 2 and min(s.values()) >= 0.2 * max(s.values())) / markets

    first_buy, flipped, sold = {}, set(), set()
    for r in trades:
        if r["side"] == "BUY":
            first_buy.setdefault(r["asset"], r["timestamp"])
        elif r["asset"] in first_buy:
            sold.add(r["asset"])  # winners redeem rather than sell, so any sale = exited before resolution
            if r["timestamp"] - first_buy[r["asset"]] <= QUICK_FLIP_HOURS * 3600:
                flipped.add(r["asset"])
    quick_flip = len(flipped) / len(first_buy)
    early_exit = len(sold) / len(first_buy)

    buy_usd = sum(usd(r) for r in buys) or 1.0
    high_price = sum(usd(r) for r in buys if float(r["price"]) >= 0.90) / buy_usd
    speed = sum(usd(r) for r in buys
                if any(p in (r.get("title", "") + r.get("eventSlug", "")).lower()
                       for p in SPEED_PATTERNS)) / buy_usd
    merge_rate = len({r["conditionId"] for r in rows if r.get("type") == "MERGE"}) / markets
    reward_usd = sum(usd(r) for r in rows if r.get("type") == "REWARD")

    if fills_per_day > MAX_FILLS_PER_DAY:
        reasons.append("bot (trades too often)")
    if two_sided > MAX_TWO_SIDED:
        reasons.append("market maker (both sides)")
    if merge_rate > MAX_MERGE_RATE:
        reasons.append("arbitrage (merges)")
    if quick_flip > MAX_QUICK_FLIP:
        reasons.append("scalper (quick flips)")
    if high_price > MAX_HIGH_PRICE_SHARE:
        reasons.append("90c+ grinder")
    if speed > MAX_SPEED_SHARE:
        reasons.append("speed markets")
    return {"fills_per_day": fills_per_day, "two_sided": two_sided, "quick_flip": quick_flip,
            "high_price": high_price, "speed": speed, "merge_rate": merge_rate, "early_exit": early_exit,
            "reward_usd": reward_usd}, reasons


def edge(closed, now):
    pos = []
    for p in closed:
        shares, price = float(p.get("totalBought") or 0), float(p.get("avgPrice") or 0)
        if shares > 0 and price > 0:
            pos.append({"cost": shares * price, "pnl": float(p.get("realizedPnl") or 0),
                        "price": price, "ts": p["timestamp"]})
    if len(pos) < MIN_CLOSED:
        return {}, [f"too little history ({len(pos)} closed bets)"]

    def stats(group):
        if not group:
            return {"n": 0, "pnl": 0.0, "roi": 0.0, "edge": 0.0}
        cost = sum(p["cost"] for p in group)
        pnl = sum(p["pnl"] for p in group)
        win_rate = statistics.fmean(1.0 if p["pnl"] > 0 else 0.0 for p in group)
        return {"n": len(group), "pnl": pnl, "roi": pnl / cost if cost else 0.0,
                # how much more often they win than the price they paid implies
                "edge": win_rate - statistics.fmean(p["price"] for p in group)}

    avg_cost = statistics.fmean(p["cost"] for p in pos)
    allp = stats(pos)
    big = stats([p for p in pos if p["cost"] > avg_cost])
    small = stats([p for p in pos if p["cost"] <= avg_cost])
    reasons = []
    if allp["roi"] <= 0:
        reasons.append("not profitable over lookback")
    elif big["n"] < MIN_BIG:
        reasons.append(f"too few big bets ({big['n']})")
    elif big["roi"] <= 0:
        reasons.append("big bets lose money")

    # One massive win: drop their best trade and see if they're still profitable.
    best = max(pos, key=lambda p: p["pnl"])
    rest_cost = sum(p["cost"] for p in pos) - best["cost"]
    roi_without_best = (allp["pnl"] - best["pnl"]) / rest_cost if rest_cost else 0.0
    top_share = best["pnl"] / allp["pnl"] if allp["pnl"] > 0 else 1.0
    if not reasons and roi_without_best <= 0:
        reasons.append("one big win (unprofitable without it)")
    elif not reasons and top_share > MAX_TOP_WIN_SHARE:
        reasons.append(f"one big win ({top_share:.0%} of profit)")

    # Consistency: profitable in enough separate 30-day blocks, not one hot streak.
    months = max(1, LOOKBACK_DAYS // 30)
    month_pnl = [0.0] * months
    for p in pos:
        m = min(months - 1, int((now - p["ts"]) // (30 * 86400)))
        month_pnl[m] += p["pnl"]
    good_months = sum(1 for x in month_pnl if x > 0)
    if not reasons and good_months < min(MIN_PROFITABLE_MONTHS, months):
        reasons.append(f"inconsistent (profitable {good_months} of {months} months)")

    return {"all": allp, "big": big, "small": small, "good_months": good_months,
            "months": months, "roi_without_best": roi_without_best, "top_share": top_share}, reasons


def clv(wallet, rows, now):
    """Average price move on their outcome in the CLV_HOURS after their big bets (positive = market followed them)."""
    bets = [b for b in group_bets(wallet, rows) if b["side"] == "BUY"]
    if not bets:
        return None, 0
    avg = statistics.fmean(b["usd"] for b in bets)
    big = [b for b in bets if b["usd"] > avg and now - b["last_ts"] > CLV_HOURS * 3600][-CLV_SAMPLES:]
    drifts = []
    for b in big:
        try:
            hist = get("/prices-history", base=CLOB, market=b["asset"], startTs=b["last_ts"],
                       endTs=b["last_ts"] + CLV_HOURS * 3600, fidelity=60).get("history", [])
        except Exception:
            continue
        if hist:
            later = float(max(hist, key=lambda x: x["t"])["p"])
            drifts.append(later - b["price"])
        time.sleep(0.1)
    return (statistics.fmean(drifts) if drifts else None), len(drifts)


def main():
    now = int(time.time())
    since = now - LOOKBACK_DAYS * 86400
    cands = candidates()
    print(f"{len(cands)} candidates")
    passed, rejected = [], Counter()

    for wallet, info in cands.items():
        name = info["name"]
        try:
            rows, capped = paged("/activity", 500, 6, since, user=wallet,
                                 sortBy="TIMESTAMP", sortDirection="DESC")
            rows.sort(key=lambda r: r["timestamp"])
            fp, reasons = fingerprint(rows, now)
            if capped and not reasons:
                reasons.append("bot (trades too often)")
            ed = {}
            if not reasons:
                closed, _ = paged("/closed-positions", 50, 10, since, user=wallet,
                                  sortBy="TIMESTAMP", sortDirection="DESC")
                ed, more = edge(closed, now)
                reasons += more
                if not reasons and fp["reward_usd"] > MAX_REWARD_SHARE * ed["all"]["pnl"]:
                    reasons.append("reward farmer")
            c, n_clv = None, 0
            if not reasons:
                c, n_clv = clv(wallet, rows, now)
                if n_clv >= MIN_CLV_SAMPLES and c < 0:
                    reasons.append("market moves against their big bets")
        except Exception as e:
            reasons = [f"fetch error ({type(e).__name__})"]

        if reasons:
            print(f"  REJECT {name}: {', '.join(reasons)}")
            rejected[reasons[0].split(" (")[0]] += 1
            continue

        big = ed["big"]
        clv_used = c if n_clv >= MIN_CLV_SAMPLES else 0.0
        # Big-bet edge plus CLV, shrunk toward zero when the sample is small.
        score = (big["edge"] + clv_used) * big["n"] / (big["n"] + 20)
        stat = f"90d big bets: ROI {big['roi']:+.0%}, edge {big['edge'] * 100:+.1f} pts"
        if n_clv >= MIN_CLV_SAMPLES:
            stat += f", CLV {c * 100:+.1f}\u00a2"
        stat += (f", profitable {ed['good_months']}/{ed['months']} months"
                 f"\nSells before resolution {fp['early_exit']:.0%} of the time"
                 + (" - watch for exit alerts" if fp["early_exit"] >= 0.4 else " - mostly holds"))
        print(f"  PASS   {name}: score {score:.3f} | {stat}")
        passed.append({"wallet": wallet, "name": name, "score": round(score, 4),
                       "boards": info["boards"], "stat": stat})

    passed.sort(key=lambda x: x["score"], reverse=True)
    chosen = passed[:MAX_WATCH]

    try:
        with open(WATCHLIST_FILE) as f:
            old = {w["wallet"] for w in json.load(f).get("wallets", [])}
    except (FileNotFoundError, json.JSONDecodeError):
        old = set()

    if not chosen:
        notify("Screener: nobody passed",
               f"Kept last week's list. Rejections: {dict(rejected)}", ["warning"])
        return

    with open(WATCHLIST_FILE, "w") as f:
        json.dump({"updated": now, "lookback_days": LOOKBACK_DAYS, "wallets": chosen}, f, indent=1)

    new_set = {w["wallet"] for w in chosen}
    lines = [f"{i}. {w['name']}{' (new)' if w['wallet'] not in old else ''} - {w['stat']}"
             for i, w in enumerate(chosen, 1)]
    why = ", ".join(f"{k} {v}" for k, v in rejected.most_common())
    notify(f"Watchlist: {len(chosen)} traders ({len(new_set - old)} new, {len(old - new_set)} dropped)",
           "\n".join(lines) + f"\n\nRejected {sum(rejected.values())}: {why}",
           ["clipboard"], priority=3)


if __name__ == "__main__":
    main()
