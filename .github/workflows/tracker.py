#!/usr/bin/env python3
"""
tracker.py - paper-trading ledger: what you'd have made betting exactly what each alert said.

  - whale_watch.py records every alert at the price you could actually have bought at (best ask),
    with the stake the alert told you to bet.
  - When the trader sells, it also records what you'd have gotten selling the same share
    of your position (best bid), so you can compare "hold to the end" vs "follow their exits".
  - Every watcher run checks open bets; when a market resolves you get a WON/LOST ping with
    your running total.
  - `python tracker.py` sends a full summary (the weekly screener workflow runs it).

ledger.csv is committed to the repo so the history is never lost. Set LEDGER_FILE to an empty
string on any second copy of the watcher (e.g. your laptop) so bets aren't recorded twice.
"""
import csv
import json
import os
import time
from collections import defaultdict
from datetime import datetime, timezone

import whale_watch as ww

# Not ww.env(): whale_watch imports this module, so it may not be fully loaded yet.
LEDGER_FILE = os.environ.get("LEDGER_FILE", "ledger.csv").strip()
GAMMA = "https://gamma-api.polymarket.com"
# Used only if a market's fee schedule can't be read (0.05 = sports/culture/economics rate).
DEFAULT_FEE_RATE = float(os.environ.get("DEFAULT_FEE_RATE", "0.05"))
FIELDS = ["id", "alerted_at", "trader", "wallet", "market", "outcome", "asset", "condition_id",
          "outcome_index", "event_slug", "their_price", "entry_price", "stake", "shares",
          "fee_rate", "fee_exp", "fees_paid", "status", "resolved_at", "payout", "pnl_hold", "followed_frac", "follow_proceeds", "exit_fees",
          "pnl_follow", "close_price", "clv_cents"]

_rows = None
_dirty = False
_fees = {}


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M")


def _f(row, key, default=0.0):
    try:
        return float(row.get(key) or default)
    except ValueError:
        return default


def rows():
    global _rows
    if _rows is None:
        try:
            with open(LEDGER_FILE, newline="") as f:
                _rows = list(csv.DictReader(f))
        except FileNotFoundError:
            _rows = []
    return _rows


def save():
    global _dirty
    if not (LEDGER_FILE and _dirty):
        return
    with open(LEDGER_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows())
    _dirty = False


def book(token_id):
    """(best bid, best ask) for an outcome token, None where unavailable."""
    try:
        b = ww.get("/book", base=ww.CLOB, token_id=token_id)
    except Exception:
        return None, None
    bids = [float(x["price"]) for x in b.get("bids", [])]
    asks = [float(x["price"]) for x in b.get("asks", [])]
    return (max(bids) if bids else None), (min(asks) if asks else None)


def fee_schedule(condition_id):
    """(rate, exponent) of the taker fee on this market, read from its Gamma feeSchedule.
    Polymarket: fee = shares x rate x (price x (1 - price)) ^ exponent. Makers pay nothing,
    but copying an alert means buying at the ask, i.e. you're the taker."""
    if condition_id not in _fees:
        m = _market(condition_id)
        fs = (m or {}).get("feeSchedule")
        if isinstance(fs, str):
            try:
                fs = json.loads(fs)
            except ValueError:
                fs = None
        if fs and fs.get("rate") is not None:
            sched = (float(fs["rate"]), float(fs.get("exponent") or 1))
        elif m is not None and m.get("feesEnabled") is False:
            sched = (0.0, 1.0)               # fee-free market (e.g. geopolitics)
        else:
            sched = (DEFAULT_FEE_RATE, 1.0)  # unknown: assume a typical rate rather than zero
        _fees[condition_id] = sched
    return _fees[condition_id]


def fee_per_share(price, sched):
    rate, exp = sched
    return rate * (price * (1 - price)) ** exp


def buy_with_fee(stake, price, condition_id):
    """Spend `stake` dollars in total (fee included). Returns (shares, fee, schedule)."""
    sched = fee_schedule(condition_id)
    fps = fee_per_share(price, sched)
    shares = stake / (price + fps)
    return shares, round(shares * fps, 5), sched


# ------------------------------------------------------------------ called by whale_watch
def record_entry(p, entry_price, now):
    global _dirty
    if not LEDGER_FILE or any(r["id"] == p["id"] for r in rows()):
        return
    shares, fee, (rate, exp) = buy_with_fee(p["stake"], entry_price, p["conditionId"])
    rows().append({
        "id": p["id"], "alerted_at": _iso(now), "trader": p["name"], "wallet": p["wallet"],
        "market": p["title"], "outcome": p["outcome"], "asset": p["asset"],
        "condition_id": p["conditionId"], "outcome_index": p["outcomeIndex"],
        "event_slug": p.get("eventSlug", ""), "their_price": round(p["price"], 4),
        "entry_price": round(entry_price, 4), "stake": p["stake"],
        "shares": round(shares, 4), "fee_rate": rate, "fee_exp": exp,
        "fees_paid": round(fee, 4), "status": "open",
        "followed_frac": 0, "follow_proceeds": 0})
    _dirty = True


def record_exit(wallet, asset, cum_frac, now):
    """Trader has now sold cum_frac of their position: sell the same share of yours at best bid."""
    global _dirty
    if not LEDGER_FILE:
        return
    for r in rows():
        if r["status"] != "open" or r["wallet"] != wallet or r["asset"] != asset:
            continue
        done, target = _f(r, "followed_frac"), min(1.0, cum_frac)
        if target <= done:
            continue
        bid, _ = book(asset)
        if bid is None:
            continue
        sell_shares = (target - done) * _f(r, "shares")
        sched = (_f(r, "fee_rate", DEFAULT_FEE_RATE), _f(r, "fee_exp", 1.0))
        sell_fee = sell_shares * fee_per_share(bid, sched)  # selling into the bid is a taker trade too
        r["follow_proceeds"] = round(_f(r, "follow_proceeds") + sell_shares * bid - sell_fee, 4)
        r["exit_fees"] = round(_f(r, "exit_fees") + sell_fee, 4)
        r["followed_frac"] = round(target, 4)
        _dirty = True


def _market(condition_id):
    for extra in ({}, {"closed": "true"}):
        try:
            res = ww.get("/markets", base=GAMMA, condition_ids=condition_id, **extra)
        except Exception:
            return None
        if res:
            return res[0]
    return None


def _payout(m, idx):
    """Settlement value per share (1 win, 0 loss, 0.5 for 50/50) or None if not resolved yet."""
    if not m or not m.get("closed"):
        return None
    prices = m.get("outcomePrices")
    if isinstance(prices, str):
        prices = json.loads(prices)
    try:
        p = float(prices[idx])
    except (TypeError, IndexError, ValueError):
        return None
    if p >= 0.99:
        return 1.0
    if p <= 0.01:
        return 0.0
    return p if m.get("umaResolutionStatus") == "resolved" else None


def totals():
    done = [r for r in rows() if r["status"] != "open"]
    return (sum(_f(r, "pnl_hold") for r in done), len(done),
            sum(1 for r in done if r["status"] == "won"), sum(1 for r in done if r["status"] == "lost"))


def settle(state, now):
    """Each watcher run: remember the latest price of open bets and settle resolved ones."""
    global _dirty
    if not LEDGER_FILE:
        return
    marks = state.setdefault("marks", {})
    for r in rows():
        if r["status"] != "open":
            continue
        payout = _payout(_market(r["condition_id"]), int(r["outcome_index"]))
        if payout is None:
            bid, ask = book(r["asset"])
            if bid is not None and ask is not None:
                marks[r["id"]] = round((bid + ask) / 2, 4)  # the last one seen = closing line
            continue

        shares, stake, ff = _f(r, "shares"), _f(r, "stake"), _f(r, "followed_frac")
        pnl_hold = shares * payout - stake
        pnl_follow = _f(r, "follow_proceeds") + (1 - ff) * shares * payout - stake
        close = marks.pop(r["id"], None)
        r.update({"status": "won" if payout == 1 else "lost" if payout == 0 else "void",
                  "resolved_at": _iso(now), "payout": payout, "pnl_hold": round(pnl_hold, 2),
                  "pnl_follow": round(pnl_follow, 2),
                  "close_price": "" if close is None else close,
                  "clv_cents": "" if close is None else round((close - _f(r, "entry_price")) * 100, 1)})
        _dirty = True

        total, n, wins, losses = totals()
        lines = [r["market"], f"Bet ${stake:,.0f} on {r['outcome']} @ {ww.cents(_f(r, 'entry_price'))}"
                 f" (incl. ${_f(r, 'fees_paid'):,.2f} fee)"]
        if ff > 0:
            lines.append(f"Following their exit instead: {pnl_follow:+,.2f}"
                         f" (incl. ${_f(r, 'exit_fees'):,.2f} selling fee)")
        if close is not None:
            lines.append(f"Closing line {ww.cents(close)} ({r['clv_cents']:+}\u00a2 vs your entry)")
        lines.append(f"Running total: {total:+,.2f} over {n} bets ({wins}-{losses})")
        ww.notify(f"{r['status'].upper()} {pnl_hold:+,.2f}: {r['trader']} - {r['outcome']}",
                  "\n".join(lines), ["moneybag" if pnl_hold > 0 else "x"], priority=3)


# ------------------------------------------------------------------ summary report
def summary():
    all_rows = rows()
    if not all_rows:
        ww.notify("Paper trading: no bets yet", "Nothing recorded so far.", ["bar_chart"])
        return
    done = [r for r in all_rows if r["status"] != "open"]
    open_ = [r for r in all_rows if r["status"] == "open"]
    staked = sum(_f(r, "stake") for r in done)
    hold = sum(_f(r, "pnl_hold") for r in done)
    follow = sum(_f(r, "pnl_follow") for r in done)
    wins = sum(1 for r in done if r["status"] == "won")
    clvs = [_f(r, "clv_cents") for r in done if r.get("clv_cents") not in ("", None)]
    fees = sum(_f(r, "fees_paid") for r in all_rows)

    lines = [f"Settled: {len(done)} bets, {wins}-{len(done) - wins}, ${staked:,.0f} staked"]
    if done:
        lines.append(f"Hold to the end: {hold:+,.2f} (ROI {hold / staked:+.1%})" if staked else "")
        lines.append(f"Follow their exits: {follow:+,.2f}")
        exit_fees = sum(_f(r, "exit_fees") for r in done)
        lines.append(f"Fees paid: ${fees:,.2f} buying, +${exit_fees:,.2f} more if following exits "
                     "(already included above)")
    if clvs:
        beat = sum(1 for c in clvs if c > 0) / len(clvs)
        lines.append(f"Closing line: avg {sum(clvs) / len(clvs):+.1f}\u00a2, beat it {beat:.0%} of the time")

    if open_:
        open_stake = sum(_f(r, "stake") for r in open_)
        value = 0.0
        for r in open_[:40]:
            bid, ask = book(r["asset"])
            mark = (bid + ask) / 2 if bid is not None and ask is not None else _f(r, "entry_price")
            value += _f(r, "shares") * mark
            time.sleep(0.1)
        lines.append(f"Open: {len(open_)} bets, ${open_stake:,.0f} staked, worth ~${value:,.0f} now")

    by_trader = defaultdict(lambda: [0.0, 0])
    for r in done:
        by_trader[r["trader"]][0] += _f(r, "pnl_hold")
        by_trader[r["trader"]][1] += 1
    if by_trader:
        lines.append("\nBy trader:")
        for name, (pnl, n) in sorted(by_trader.items(), key=lambda x: -x[1][0]):
            lines.append(f"{name}: {pnl:+,.2f} ({n} bets)")

    title = f"Paper trading: {hold:+,.2f}" + (f" (ROI {hold / staked:+.1%})" if staked else "")
    ww.notify(title, "\n".join(x for x in lines if x), ["bar_chart"], priority=3)


if __name__ == "__main__":
    summary()
