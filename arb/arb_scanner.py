#!/usr/bin/env python3
"""
Kalshi <-> Polymarket US arbitrage scanner. ALERT-ONLY: it never places trades.

  python arb_scanner.py scan       # price-check the pairs in pairs.json, ntfy on edges
  python arb_scanner.py discover   # suggest possible matching markets -> candidates.csv

The idea: if one side of a market is cheap on one exchange and the other side is
cheap on the other, buying both costs less than the $1 the winning side pays out.
"""
import argparse
import csv
import difflib
import json
import math
import os
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import requests

# ---------- settings (override with environment variables) ----------
KALSHI_BASE = os.getenv("KALSHI_BASE", "https://api.elections.kalshi.com/trade-api/v2")
NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh")
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "")
MIN_EDGE = float(os.getenv("MIN_EDGE", "0.02"))        # $ profit per contract after fees
SIZE = int(os.getenv("SIZE", "100"))                   # contracts per leg (for fee rounding)
KALSHI_THETA = float(os.getenv("KALSHI_THETA", "0.07"))  # Kalshi taker fee coefficient
PM_US_THETA = float(os.getenv("PM_US_THETA", "0.06"))    # Polymarket US taker fee coefficient
COOLDOWN_MIN = int(os.getenv("COOLDOWN_MIN", "60"))    # don't repeat the same alert sooner
HERE = Path(__file__).resolve().parent
PAIRS_FILE = Path(os.getenv("PAIRS_FILE", HERE / "pairs.json"))
STATE_FILE = Path(os.getenv("STATE_FILE", HERE / "state" / "seen.json"))
CANDIDATES_FILE = Path(os.getenv("CANDIDATES_FILE", HERE / "candidates.csv"))


# ---------- helpers ----------
def to_price(x):
    """Turn an API price (number, string, or {"value": "0.55"}) into dollars 0-1."""
    if isinstance(x, dict):
        x = x.get("value", x.get("price"))
    try:
        p = float(x)
    except (TypeError, ValueError):
        return None
    return p if 0.0 < p < 1.0 else None


def kalshi_fee(p, n=SIZE):
    """Kalshi taker fee per contract: ceil-to-cent(theta * C * p * (1-p)) / C."""
    return math.ceil(KALSHI_THETA * n * p * (1 - p) * 100 - 1e-9) / 100 / n


def pm_fee(p, n=SIZE):
    """Polymarket US taker fee per contract: theta * p * (1-p)."""
    return PM_US_THETA * p * (1 - p)


# ---------- Kalshi (public market data, no login needed) ----------
def kalshi_get(path, params=None):
    r = requests.get(f"{KALSHI_BASE}{path}", params=params, timeout=20)
    r.raise_for_status()
    return r.json()


def kalshi_price(m, field):
    v = m.get(f"{field}_dollars")          # newer API: "0.4500"
    if v is not None:
        return to_price(v)
    v = m.get(field)                       # older API: cents as int
    return to_price(v / 100) if isinstance(v, (int, float)) else None


def kalshi_title(m):
    title = m.get("title") or m.get("ticker", "")
    sub = m.get("yes_sub_title") or m.get("subtitle") or ""
    return f"{title} — {sub}" if sub and sub.lower() not in title.lower() else title


def kalshi_quote(ticker):
    m = kalshi_get(f"/markets/{ticker}")["market"]
    yes_ask = kalshi_price(m, "yes_ask")
    no_ask = kalshi_price(m, "no_ask")
    yes_bid = kalshi_price(m, "yes_bid")
    if no_ask is None and yes_bid is not None:
        no_ask = round(1 - yes_bid, 4)
    return {"status": m.get("status"), "yes_ask": yes_ask, "no_ask": no_ask,
            "title": kalshi_title(m)}


def kalshi_all_open(limit_total=20000):
    out, cursor = [], None
    while True:
        params = {"status": "open", "limit": 1000, "mve_filter": "exclude"}
        if cursor:
            params["cursor"] = cursor
        d = kalshi_get("/markets", params)
        out += d.get("markets", [])
        cursor = d.get("cursor")
        if not cursor or len(out) >= limit_total:
            return out


# ---------- Polymarket US (official SDK, public endpoints, no login needed) ----------
def pm_client():
    from polymarket_us import PolymarketUS
    return PolymarketUS()


def pm_quote(client, slug):
    """Best bid/offer for the YES ("long") side. Buying NO ("short") costs 1 - best YES bid."""
    md = client.markets.bbo(slug)["marketData"]
    bid, ask = to_price(md.get("bestBid")), to_price(md.get("bestAsk"))
    return {"yes_ask": ask, "no_ask": round(1 - bid, 4) if bid else None}


def pm_title(m):
    return m.get("question") or m.get("title") or m.get("slug", "")


def pm_all_markets(limit_total=10000):
    client, out, offset = pm_client(), [], 0
    try:
        while len(out) < limit_total:
            page = client.markets.list({"limit": 100, "offset": offset, "active": True})
            ms = page.get("markets", [])
            out += [m for m in ms if not m.get("closed")]
            if len(ms) < 100:
                break
            offset += 100
    finally:
        client.close()
    return out


# ---------- the arbitrage math ----------
def check_pair(k, pm):
    """Return both ways of covering YES+NO across the two exchanges."""
    combos = [
        ("kyes_pmno", "YES on Kalshi", k["yes_ask"], kalshi_fee, "NO on Polymarket US", pm["no_ask"], pm_fee),
        ("kno_pmyes", "NO on Kalshi", k["no_ask"], kalshi_fee, "YES on Polymarket US", pm["yes_ask"], pm_fee),
    ]
    results = []
    for side, la, a, fa, lb, b, fb in combos:
        if a is None or b is None:
            continue
        cost = a + b + fa(a) + fb(b)
        results.append({"side": side, "leg_a": f"{la} @ {a:.2f}", "leg_b": f"{lb} @ {b:.2f}",
                        "cost": cost, "edge": 1 - cost})
    return results


# ---------- alerts + memory between runs ----------
def notify(title, body):
    if not NTFY_TOPIC:
        print(f"[ALERT, no NTFY_TOPIC set] {title}\n{body}")
        return
    requests.post(f"{NTFY_SERVER}/{NTFY_TOPIC}", data=body.encode("utf-8"),
                  headers={"Title": title, "Priority": "high", "Tags": "moneybag"}, timeout=15)


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=1))


def should_alert(state, key, edge):
    prev = state.get(key)
    if not prev:
        return True
    if time.time() - prev["t"] > COOLDOWN_MIN * 60:
        return True
    return edge >= prev["edge"] + 0.01   # re-alert early if it got 1¢+ better


# ---------- modes ----------
def scan():
    pairs = [p for p in json.loads(PAIRS_FILE.read_text()) if p.get("enabled", True)]
    if not pairs:
        print("No enabled pairs in pairs.json. Run `discover` and add some.")
        return
    client, state, alerts = pm_client(), load_state(), 0
    try:
        for pair in pairs:
            label = pair.get("label", pair["kalshi_ticker"])
            try:
                k = kalshi_quote(pair["kalshi_ticker"])
                pm = pm_quote(client, pair["pm_slug"])
            except Exception as e:  # one bad pair shouldn't kill the run
                print(f"[skip] {label}: {e}")
                continue
            if k["status"] not in ("open", "active"):
                print(f"[skip] {label}: Kalshi status is {k['status']}")
                continue
            if pair.get("inverted"):  # Kalshi YES == Polymarket NO
                pm = {"yes_ask": pm["no_ask"], "no_ask": pm["yes_ask"]}
            for r in check_pair(k, pm):
                print(f"{label[:45]:45} | {r['leg_a']:20} + {r['leg_b']:28} "
                      f"cost {r['cost']:.3f}  edge {r['edge']:+.3f}")
                key = f"{pair['kalshi_ticker']}|{pair['pm_slug']}|{r['side']}"
                if r["edge"] >= MIN_EDGE and should_alert(state, key, r["edge"]):
                    notify(f"Arb {r['edge'] * 100:.1f}c/contract: {label}"[:120],
                           f"Buy {r['leg_a']}\n+ Buy {r['leg_b']}\n"
                           f"Cost after fees: ${r['cost']:.3f} -> ${r['edge']:.3f} per $1 payout\n"
                           f"Top-of-book only. Check size and that both resolve the same way first.")
                    state[key] = {"t": time.time(), "edge": r["edge"]}
                    alerts += 1
    finally:
        client.close()
        save_state(state)
    print(f"Done. {len(pairs)} pairs checked, {alerts} alert(s) sent.")


STOP = set("will the a an of in on by to be is for at and or vs before after than "
           "this that win wins game match yes no 2025 2026 2027".split())


def tokens(text):
    return [t for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in STOP and len(t) > 1]


def discover(min_score=0.55, top_print=40):
    print("Fetching Kalshi open markets...")
    km = kalshi_all_open()
    print(f"  {len(km)} Kalshi markets")
    print("Fetching Polymarket US markets...")
    pmm = pm_all_markets()
    print(f"  {len(pmm)} Polymarket US markets")

    k_titles = [kalshi_title(m) for m in km]
    k_tokens = [set(tokens(t)) for t in k_titles]
    doc_freq = Counter(t for ts in k_tokens for t in ts)
    index = defaultdict(list)
    for i, ts in enumerate(k_tokens):
        for t in ts:
            if doc_freq[t] < 300:          # skip super-common words
                index[t].append(i)

    rows = []
    for m in pmm:
        title = pm_title(m)
        pts = set(tokens(title))
        hits = Counter(i for t in pts for i in index.get(t, []))
        for i, shared in hits.most_common(25):
            if shared < 2:
                break
            jacc = shared / len(pts | k_tokens[i])
            ratio = difflib.SequenceMatcher(None, title.lower(), k_titles[i].lower()).ratio()
            score = 0.6 * jacc + 0.4 * ratio
            if score >= min_score:
                rows.append((round(score, 3), km[i]["ticker"], k_titles[i], m.get("slug", ""), title))

    rows.sort(reverse=True)
    with CANDIDATES_FILE.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["score", "kalshi_ticker", "kalshi_title", "pm_slug", "pm_title"])
        w.writerows(rows)
    print(f"\n{len(rows)} candidate pairs written to {CANDIDATES_FILE.name}. Top {top_print}:\n")
    for r in rows[:top_print]:
        print(f"{r[0]:.2f}  {r[1]}  <->  {r[3]}\n      K: {r[2][:90]}\n      P: {r[4][:90]}")
    print("\nThese are GUESSES. Check both rulebooks before adding any to pairs.json.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["scan", "discover"])
    mode = ap.parse_args().mode
    sys.exit(scan() if mode == "scan" else discover())
