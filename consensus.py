#!/usr/bin/env python3
"""
bethub consensus alerts

Pings your phone (via ntfy) when 2+ of the traders you follow hold the SAME
side of the SAME market. Built to keep your bet count low: max 3 alerts a day.

Filters:
  - a position only counts if it's big FOR THAT TRADER (vs. their typical bet)
  - traders holding both sides of a market (hedged) are ignored for it
  - markets where your traders disagree (opposite sides) are skipped
  - skips if the price has run more than MAX_PRICE_DRIFT above their entry
  - only near-term, unsettled markets
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------- traders --
# Pulled automatically from Polymarket's leaderboard every run. Nothing to paste.
LEADERBOARD_SIZE = 10
LEADERBOARD_PERIOD = "MONTH"       # DAY, WEEK, MONTH, or ALL
LEADERBOARD_CATEGORY = "OVERALL"   # or "SPORTS" for sports-only traders
# Always followed, even if they drop off the leaderboard:
ALWAYS_FOLLOW = {
    "HomeRunHazard": "0x5268527977f700f9bf9b6d5cd843859e4e70135d",
}

# --------------------------------------------------------------- settings --
UNIT = 10                    # your 1 unit, in dollars
MIN_TRADERS = 2              # how many traders must agree
RELATIVE_SIZE = 0.5          # a position counts if it's >= 50% of that trader's typical bet
MIN_POSITION_FLOOR = 500     # ...and never below this many dollars
DUST_USD = 100               # ignore scraps under this when working out "typical"
MAX_ALERTS_PER_DAY = None    # None = no cap; set a number (e.g. 3) to cap alerts per day
MAX_PRICE_DRIFT = 0.02       # skip if price is >2¢ above their average entry
MIN_PRICE, MAX_PRICE = 0.05, 0.95   # skip markets that are basically decided
MAX_DAYS_OUT = 3             # only markets ending within this many days

TIMEZONE = os.getenv("TZ_NAME", "America/New_York")   # when your "day" resets
STATE_FILE = os.getenv("STATE_FILE", "consensus_state.json")
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "")
NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh")
DRY_RUN = os.getenv("DRY_RUN") == "1"   # print alerts instead of sending

API = "https://data-api.polymarket.com/positions"
LEADERBOARD_API = "https://data-api.polymarket.com/v1/leaderboard"
PAGE = 100


# ------------------------------------------------------------------ fetch --
def get_json(url, tries=3):
    req = urllib.request.Request(url, headers={"User-Agent": "bethub-consensus/1.0"})
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.load(r)
        except Exception:
            if attempt == tries - 1:
                raise
            time.sleep(2 * (attempt + 1))


def get_traders():
    """Top N leaderboard wallets, plus anyone in ALWAYS_FOLLOW."""
    traders = dict(ALWAYS_FOLLOW)
    known = {w.lower() for w in traders.values()}
    q = urllib.parse.urlencode({
        "category": LEADERBOARD_CATEGORY, "timePeriod": LEADERBOARD_PERIOD,
        "orderBy": "PNL", "limit": LEADERBOARD_SIZE,
    })
    try:
        rows = get_json(f"{LEADERBOARD_API}?{q}") or []
    except Exception as e:
        print(f"! couldn't load leaderboard ({e}); using ALWAYS_FOLLOW only", file=sys.stderr)
        return traders
    for row in rows:
        wallet = row.get("proxyWallet") or ""
        if not wallet or wallet.lower() in known:
            continue
        name = row.get("userName") or wallet[:8]
        if name in traders:
            name = f"{name}-{wallet[2:6]}"
        traders[name] = wallet
        known.add(wallet.lower())
    print(f"Following {len(traders)} traders: {', '.join(traders)}")
    return traders


def fetch_positions(wallet):
    """All open positions for one wallet, paged."""
    out, offset = [], 0
    while offset < 3000:
        q = urllib.parse.urlencode(
            {"user": wallet, "limit": PAGE, "offset": offset, "sizeThreshold": 1}
        )
        page = get_json(f"{API}?{q}") or []
        out.extend(page)
        if len(page) < PAGE:
            break
        offset += PAGE
    return out


# ------------------------------------------------------------------ logic --
def days_until(end):
    if not end:
        return None
    try:
        d = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return (d - datetime.now(timezone.utc)).total_seconds() / 86400


def typical_bet(positions):
    """Median cost of a trader's open positions, ignoring tiny scraps."""
    costs = sorted(
        float(p.get("initialValue") or 0) for p in positions
        if not p.get("redeemable") and float(p.get("initialValue") or 0) >= DUST_USD
    )
    if not costs:
        return 0.0
    mid = len(costs) // 2
    return costs[mid] if len(costs) % 2 else (costs[mid - 1] + costs[mid]) / 2


def find_consensus(all_positions):
    """all_positions: {trader_name: [position, ...]} -> ranked list of picks."""
    sides = defaultdict(dict)        # (conditionId, outcomeIndex) -> {name: pos}
    typical = {}
    for name, positions in all_positions.items():
        typical[name] = typical_bet(positions)
        threshold = max(MIN_POSITION_FLOOR, typical[name] * RELATIVE_SIZE)
        print(f"{name}: typical bet ${typical[name]:,.0f}, counting bets >= ${threshold:,.0f}")
        held = defaultdict(dict)     # conditionId -> {outcomeIndex: pos}
        for p in positions:
            if p.get("redeemable"):
                continue
            if float(p.get("initialValue") or 0) < threshold:
                continue
            held[p["conditionId"]][p.get("outcomeIndex")] = p
        for cond, by_idx in held.items():
            if len(by_idx) > 1:      # holds both sides = hedged, ignore
                continue
            idx, p = next(iter(by_idx.items()))
            sides[(cond, idx)][name] = p

    sides_per_market = defaultdict(set)
    for cond, idx in sides:
        sides_per_market[cond].add(idx)

    picks = []
    for (cond, idx), holders in sides.items():
        if len(holders) < MIN_TRADERS:
            continue
        if len(sides_per_market[cond]) > 1:   # your traders disagree
            continue

        sample = next(iter(holders.values()))
        cur = float(sample.get("curPrice") or 0)
        if not (MIN_PRICE <= cur <= MAX_PRICE):
            continue
        left = days_until(sample.get("endDate"))
        if left is not None and (left < -1 or left > MAX_DAYS_OUT):
            continue

        cost = sum(float(p["initialValue"]) for p in holders.values())
        shares = sum(float(p["size"]) for p in holders.values())
        avg_entry = cost / shares if shares else cur
        if cur > avg_entry + MAX_PRICE_DRIFT:
            continue

        units = 1.5 if len(holders) >= 3 else 1.0
        picks.append({
            "key": f"{cond}:{idx}",
            "title": sample.get("title", "?"),
            "outcome": sample.get("outcome", "?"),
            "event_slug": sample.get("eventSlug") or sample.get("slug") or "",
            "cur": cur,
            "avg_entry": avg_entry,
            "total": cost,
            "n": len(holders),
            "units": units,
            "stake": UNIT * units,
            "holders": sorted(
                ((n, float(p["initialValue"]), float(p.get("avgPrice") or 0),
                  float(p["initialValue"]) / typical[n] if typical[n] else 0)
                 for n, p in holders.items()),
                key=lambda h: -h[1],
            ),
        })

    picks.sort(key=lambda x: (x["n"], x["total"]), reverse=True)
    return picks


# ------------------------------------------------------------------ alert --
def cents(x):
    return f"{x * 100:.1f}¢"


def send(pick, n_today):
    title = f"{pick['n']} traders agree: {pick['outcome']}"
    lines = [pick["title"]]
    for name, cost, avg, rel in pick["holders"]:
        lines.append(f"• {name}: ${cost:,.0f} @ {cents(avg)} ({rel:.1f}x their usual)")
    lines.append(f"Now {cents(pick['cur'])} (their avg {cents(pick['avg_entry'])})")
    lines.append(f"Your stake: ${pick['stake']:g} ({pick['units']:g}u)")
    lines.append(f"Don't pay more than {cents(pick['avg_entry'] + MAX_PRICE_DRIFT)}")
    if MAX_ALERTS_PER_DAY:
        lines.append(f"Pick {n_today} of {MAX_ALERTS_PER_DAY} today")
    else:
        lines.append(f"Pick #{n_today} today")
    body = "\n".join(lines)

    if DRY_RUN or not NTFY_TOPIC:
        print(f"--- {title}\n{body}\n")
        return

    headers = {
        "Title": title.encode("latin-1", "ignore").decode("latin-1"),
        "Tags": "handshake,moneybag",
        "Priority": "high",
    }
    if pick["event_slug"]:
        headers["Click"] = f"https://polymarket.com/event/{pick['event_slug']}"
    req = urllib.request.Request(
        f"{NTFY_SERVER}/{NTFY_TOPIC}", data=body.encode("utf-8"),
        headers=headers, method="POST",
    )
    urllib.request.urlopen(req, timeout=15).read()


# ------------------------------------------------------------------ state --
def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"day": "", "sent_today": 0, "alerted": {}}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)


# ------------------------------------------------------------------- main --
def main():
    today = datetime.now(ZoneInfo(TIMEZONE)).date()
    state = load_state()
    if state.get("day") != today.isoformat():
        state["day"], state["sent_today"] = today.isoformat(), 0
    cutoff = (today - timedelta(days=7)).isoformat()
    state["alerted"] = {k: d for k, d in state.get("alerted", {}).items() if d >= cutoff}

    all_positions = {}
    for name, wallet in get_traders().items():
        try:
            all_positions[name] = fetch_positions(wallet)
            print(f"{name}: {len(all_positions[name])} open positions")
        except Exception as e:
            print(f"! couldn't fetch {name}: {e}", file=sys.stderr)
        time.sleep(0.3)

    picks = [p for p in find_consensus(all_positions) if p["key"] not in state["alerted"]]
    if MAX_ALERTS_PER_DAY:
        room = max(MAX_ALERTS_PER_DAY - state["sent_today"], 0)
    else:
        room = len(picks)
    print(f"{len(picks)} new consensus plays, sending {min(room, len(picks))}")

    for pick in picks[:room]:
        send(pick, state["sent_today"] + 1)
        state["alerted"][pick["key"]] = today.isoformat()
        state["sent_today"] += 1

    save_state(state)


if __name__ == "__main__":
    main()
