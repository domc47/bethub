#!/usr/bin/env python3
"""
bethub consensus alerts

Pings your phone (via ntfy) when 2+ of the traders you follow hold the SAME
side of the SAME market, and warns you later if anyone takes the other side.

Filters:
  - a position only counts if it's big FOR THAT TRADER (vs. their typical bet)
  - skips the WHOLE GAME if your traders hold bets pointing opposite ways on it
    (e.g. one on Hornets, another on Pistons, at any spread or moneyline,
     or Over vs Under on the same total)
  - skips if the price has run more than MAX_PRICE_DRIFT above their entry
  - only near-term, unsettled markets

Follow-up warnings:
  - after an alert, if any trader you follow takes the opposite side of that
    bet, you get one warning ping
"""

import json
import os
import re
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
RELATIVE_SIZE = 0.5          # a bet counts toward agreement if >= 50% of that trader's typical bet
MIN_POSITION_FLOOR = 500     # ...and never below this many dollars
CONFLICT_RELATIVE_SIZE = 0.25  # an opposite bet counts as a conflict if >= 25% of their typical bet
CONFLICT_FLOOR = 250         # ...and never below this many dollars
DUST_USD = 100               # ignore scraps under this when working out "typical"
MAX_ALERTS_PER_DAY = None    # None = no cap; set a number (e.g. 3) to cap alerts per day
MAX_PRICE_DRIFT = 0.02       # skip if price is >2¢ above their average entry
MIN_PRICE, MAX_PRICE = 0.05, 0.95   # skip markets that are basically decided
MAX_DAYS_OUT = 3             # only markets ending within this many days
REMEMBER_DAYS = 7            # how long to remember alerts (no repeats, warnings)

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
def cost_of(p):
    return float(p.get("initialValue") or 0)


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
        cost_of(p) for p in positions
        if not p.get("redeemable") and cost_of(p) >= DUST_USD
    )
    if not costs:
        return 0.0
    mid = len(costs) // 2
    return costs[mid] if len(costs) % 2 else (costs[mid - 1] + costs[mid]) / 2


def direction(p):
    """
    Which way a bet points on its game: (game, axis, direction).
      - team markets (moneyline, spreads):  axis "team",  direction = team/player
      - over/under markets:                 axis "total:<market minus line>", Over/Under
      - Yes/No markets:                     only compared within that one market
    """
    game = p.get("eventSlug") or p.get("slug") or p.get("conditionId")
    outcome = (p.get("outcome") or "").strip().lower()
    title = (p.get("title") or "").strip().lower()
    if outcome in ("over", "under"):
        # "Hornets vs. Pistons: O/U 213.5" and "... O/U 215.5" are the same total;
        # player props have different titles, so they stay separate.
        return game, "total:" + re.sub(r"[\d.]+\s*$", "", title).strip(), outcome
    if outcome in ("yes", "no"):
        return game, "market:" + str(p.get("conditionId")), outcome
    return game, "team", outcome


def build_directions(all_positions, typical):
    """game -> axis -> direction -> [(trader, outcome, market title, cost)]"""
    idx = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    for name, positions in all_positions.items():
        floor = max(CONFLICT_FLOOR, typical[name] * CONFLICT_RELATIVE_SIZE)
        for p in positions:
            if p.get("redeemable") or cost_of(p) < floor:
                continue
            game, axis, d = direction(p)
            idx[game][axis][d].append((name, p.get("outcome"), p.get("title"), cost_of(p)))
    return idx


def conflicted_games(idx):
    """Games where your traders hold bets pointing opposite ways on any axis."""
    return {g for g, axes in idx.items() if any(len(dirs) > 1 for dirs in axes.values())}


def find_consensus(all_positions):
    """Returns (ranked picks, direction index)."""
    typical = {}
    sides = defaultdict(dict)        # (conditionId, outcomeIndex) -> {name: pos}
    for name, positions in all_positions.items():
        typical[name] = typical_bet(positions)
        threshold = max(MIN_POSITION_FLOOR, typical[name] * RELATIVE_SIZE)
        print(f"{name}: typical bet ${typical[name]:,.0f}, counting bets >= ${threshold:,.0f}")
        for p in positions:
            if p.get("redeemable") or cost_of(p) < threshold:
                continue
            sides[(p["conditionId"], p.get("outcomeIndex"))][name] = p

    idx = build_directions(all_positions, typical)
    blocked = conflicted_games(idx)
    if blocked:
        print(f"Skipping {len(blocked)} games with opposite bets: {', '.join(sorted(blocked))}")

    picks = []
    for (cond, oi), holders in sides.items():
        if len(holders) < MIN_TRADERS:
            continue
        sample = next(iter(holders.values()))
        game, axis, d = direction(sample)
        if game in blocked:
            continue

        cur = float(sample.get("curPrice") or 0)
        if not (MIN_PRICE <= cur <= MAX_PRICE):
            continue
        left = days_until(sample.get("endDate"))
        if left is not None and (left < -1 or left > MAX_DAYS_OUT):
            continue

        cost = sum(cost_of(p) for p in holders.values())
        shares = sum(float(p.get("size") or 0) for p in holders.values())
        avg_entry = cost / shares if shares else cur
        if cur > avg_entry + MAX_PRICE_DRIFT:
            continue

        units = 1.5 if len(holders) >= 3 else 1.0
        picks.append({
            "key": f"{cond}:{oi}",
            "game": game, "axis": axis, "dir": d,
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
                ((n, cost_of(p), float(p.get("avgPrice") or 0),
                  cost_of(p) / typical[n] if typical[n] else 0)
                 for n, p in holders.items()),
                key=lambda h: -h[1],
            ),
        })

    picks.sort(key=lambda x: (x["n"], x["total"]), reverse=True)
    return picks, idx


def find_warnings(state, idx):
    """Past alerts where a trader you follow now holds the opposite side."""
    warnings = []
    for key, info in state["alerted"].items():
        if info.get("warned") or "game" not in info:
            continue
        dirs = idx.get(info["game"], {}).get(info["axis"], {})
        against = [h for d, hs in dirs.items() if d != info["dir"] for h in hs]
        if against:
            warnings.append((key, info, against))
    return warnings


# ------------------------------------------------------------------ alert --
def cents(x):
    return f"{x * 100:.1f}¢"


def notify(title, body, tags, event_slug=""):
    if DRY_RUN or not NTFY_TOPIC:
        print(f"--- {title}\n{body}\n")
        return
    headers = {
        "Title": title.encode("latin-1", "ignore").decode("latin-1"),
        "Tags": tags,
        "Priority": "high",
    }
    if event_slug:
        headers["Click"] = f"https://polymarket.com/event/{event_slug}"
    req = urllib.request.Request(
        f"{NTFY_SERVER}/{NTFY_TOPIC}", data=body.encode("utf-8"),
        headers=headers, method="POST",
    )
    urllib.request.urlopen(req, timeout=15).read()


def send_pick(pick, n_today):
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
    notify(f"{pick['n']} traders agree: {pick['outcome']}", "\n".join(lines),
           "handshake,moneybag", pick["event_slug"])


def send_warning(info, against):
    lines = [info["title"], f"You were alerted on: {info['outcome']}", "Now on the other side:"]
    for name, outcome, market, cost in sorted(against, key=lambda h: -h[3]):
        lines.append(f"• {name}: ${cost:,.0f} on {outcome} ({market})")
    notify(f"WARNING: other side taken on {info['outcome']}", "\n".join(lines),
           "warning", info.get("event_slug", ""))


# ------------------------------------------------------------------ state --
def load_state():
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    state.setdefault("day", "")
    state.setdefault("sent_today", 0)
    # older versions stored just a date string per alert
    state["alerted"] = {
        k: (v if isinstance(v, dict) else {"day": v})
        for k, v in state.get("alerted", {}).items()
    }
    return state


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)


# ------------------------------------------------------------------- main --
def main():
    today = datetime.now(ZoneInfo(TIMEZONE)).date()
    state = load_state()
    if state["day"] != today.isoformat():
        state["day"], state["sent_today"] = today.isoformat(), 0
    cutoff = (today - timedelta(days=REMEMBER_DAYS)).isoformat()
    state["alerted"] = {k: v for k, v in state["alerted"].items() if v.get("day", "") >= cutoff}

    all_positions = {}
    for name, wallet in get_traders().items():
        try:
            all_positions[name] = fetch_positions(wallet)
            print(f"{name}: {len(all_positions[name])} open positions")
        except Exception as e:
            print(f"! couldn't fetch {name}: {e}", file=sys.stderr)
        time.sleep(0.3)

    picks, idx = find_consensus(all_positions)

    # 1) follow-up warnings on bets you were already alerted on
    for key, info, against in find_warnings(state, idx):
        send_warning(info, against)
        info["warned"] = True
    # 2) new consensus plays
    picks = [p for p in picks if p["key"] not in state["alerted"]]
    if MAX_ALERTS_PER_DAY:
        room = max(MAX_ALERTS_PER_DAY - state["sent_today"], 0)
    else:
        room = len(picks)
    print(f"{len(picks)} new consensus plays, sending {min(room, len(picks))}")

    for pick in picks[:room]:
        send_pick(pick, state["sent_today"] + 1)
        state["sent_today"] += 1
        state["alerted"][pick["key"]] = {
            "day": today.isoformat(), "game": pick["game"], "axis": pick["axis"],
            "dir": pick["dir"], "title": pick["title"], "outcome": pick["outcome"],
            "event_slug": pick["event_slug"], "warned": False,
        }

    save_state(state)


if __name__ == "__main__":
    main()
