#!/usr/bin/env python3
"""
Polymarket bet watcher.
Polls one or more traders' public activity and pushes a phone notification
(via ntfy) whenever they place a new bet. Standard library only.

  python watch.py            # check once (used by GitHub Actions)
  python watch.py --loop     # keep running, check every 30s (your own machine)

PM_USERNAMES is a comma-separated list, e.g. "homerunhazard,someoneelse".
If a username lookup ever fails, use "name=0xWALLET" for that entry.
LEADERBOARD_TOP=10 also follows the current top 10 on the leaderboard,
refreshed every check, so people who climb onto it get added automatically.
"""
import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict

# ---- Settings (override with environment variables) -------------------------
RAW_USERS   = os.getenv("PM_USERNAMES", os.getenv("PM_USERNAME", "homerunhazard"))
NTFY_TOPIC  = os.getenv("NTFY_TOPIC", "").strip()
NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
MIN_USD     = float(os.getenv("MIN_USD", "1000"))
BUYS_ONLY   = os.getenv("BUYS_ONLY", "true").lower() == "true"
STATE_FILE  = os.getenv("STATE_FILE", "state.json")
LB_TOP      = int(os.getenv("LEADERBOARD_TOP", "0") or 0)     # 0 = off
LB_PERIOD   = os.getenv("LEADERBOARD_PERIOD", "MONTH").upper() # DAY, WEEK, MONTH, ALL
LB_CATEGORY = os.getenv("LEADERBOARD_CATEGORY", "OVERALL").upper()
LB_ORDER    = os.getenv("LEADERBOARD_ORDER", "PNL").upper()    # PNL or VOL
# -----------------------------------------------------------------------------

DATA_API  = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
HEADERS   = {"User-Agent": "pm-bet-watcher/1.1", "Accept": "application/json"}
PAGE      = 500
MAX_PAGES = 6


def parse_users(raw):
    users = {}
    for part in raw.split(","):
        part = part.strip().lstrip("@")
        if not part:
            continue
        name, _, wallet = part.partition("=")
        users[name.strip()] = wallet.strip() or None
    return users


def get_json(url, params=None):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def fetch_leaderboard():
    """Current top traders as {display_name: wallet}."""
    rows = get_json(f"{DATA_API}/v1/leaderboard", {
        "category": LB_CATEGORY, "timePeriod": LB_PERIOD,
        "orderBy": LB_ORDER, "limit": LB_TOP,
    })
    if isinstance(rows, dict):
        rows = rows.get("data") or rows.get("leaderboard") or []
    top = {}
    for r in rows[:LB_TOP]:
        wallet = r.get("proxyWallet")
        if wallet:
            name = r.get("userName") or r.get("name") or wallet[:10]
            top[name] = wallet
    return top


def resolve_wallet(username):
    data = get_json(f"{GAMMA_API}/public-search",
                    {"q": username, "search_profiles": "true"})
    profiles = data.get("profiles") or []
    for p in profiles:
        names = {(p.get("name") or "").lower(), (p.get("pseudonym") or "").lower()}
        if username.lower() in names and p.get("proxyWallet"):
            return p["proxyWallet"]
    found = ", ".join(f'{p.get("name")} ({p.get("proxyWallet")})' for p in profiles[:5])
    raise RuntimeError(f"Couldn't find @{username}. Candidates: {found or 'none'}. "
                       f"Use {username}=0xWALLET in PM_USERNAMES instead.")


def load_state():
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    state.setdefault("users", {})
    return state


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def fill_key(t):
    return f'{t.get("transactionHash")}:{t.get("asset")}:{t.get("side")}:{t.get("size")}'


def fetch_new_fills(wallet, last_ts, seen):
    new = []
    for page in range(MAX_PAGES):
        batch = get_json(f"{DATA_API}/activity", {
            "user": wallet, "type": "TRADE", "limit": PAGE, "offset": page * PAGE,
        })
        if not batch:
            break
        reached_old = False
        for t in batch:
            if t.get("type", "TRADE") != "TRADE":
                continue
            ts = int(t.get("timestamp") or 0)
            if ts < last_ts or (ts == last_ts and fill_key(t) in seen):
                reached_old = True
                continue
            new.append(t)
        if reached_old or len(batch) < PAGE:
            break
    return new


def group_bets(fills):
    groups = defaultdict(lambda: {"usd": 0.0, "shares": 0.0, "ts": 0})
    for t in fills:
        k = (t.get("title") or "Unknown market", t.get("outcome") or "?",
             (t.get("side") or "").upper())
        g = groups[k]
        g["usd"] += float(t.get("usdcSize") or 0)
        g["shares"] += float(t.get("size") or 0)
        g["ts"] = max(g["ts"], int(t.get("timestamp") or 0))
        g["slug"] = t.get("eventSlug") or t.get("slug") or ""
    bets = []
    for (title, outcome, side), g in groups.items():
        if BUYS_ONLY and side != "BUY":
            continue
        if g["usd"] < MIN_USD:
            continue
        avg = g["usd"] / g["shares"] if g["shares"] else 0
        bets.append({**g, "title": title, "outcome": outcome, "side": side, "avg": avg})
    return sorted(bets, key=lambda b: b["usd"], reverse=True)


def notify(title, message, click=None):
    print(f"\n🔔 {title}\n{message}\n")
    if not NTFY_TOPIC:
        return
    payload = {"topic": NTFY_TOPIC, "title": title, "message": message,
               "tags": ["moneybag"], "priority": 4}
    if click:
        payload["click"] = click
    req = urllib.request.Request(NTFY_SERVER, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as e:
        print(f"Notification failed: {e}", file=sys.stderr)


def describe(b):
    verb = "bought" if b["side"] == "BUY" else "sold"
    return f'{verb} ${b["usd"]:,.0f} of {b["outcome"]} @ {b["avg"]*100:.1f}¢ — {b["title"]}'


def check_user(username, wallet_override, us, quiet_start=False):
    if not us.get("wallet"):
        us["wallet"] = wallet_override or resolve_wallet(username)
        print(f"Watching @{username} → {us['wallet']}")
    last_ts, seen = int(us.get("last_ts", 0)), set(us.get("seen", []))

    fills = fetch_new_fills(us["wallet"], last_ts, seen)
    first_run = "last_ts" not in us

    if fills:
        newest = max(int(t.get("timestamp") or 0) for t in fills)
        at_newest = {fill_key(t) for t in fills if int(t.get("timestamp") or 0) == newest}
        us["last_ts"] = newest
        us["seen"] = sorted(at_newest | (seen if newest == last_ts else set()))
    elif first_run:
        us["last_ts"] = int(time.time())
        us["seen"] = []

    if first_run:
        if quiet_start:
            print(f"Now following @{username} from the leaderboard")
        else:
            notify(f"Watching @{username}", "Watcher is live. You'll get a ping on new bets.")
        return

    bets = group_bets(fills)
    print(f"{time.strftime('%H:%M:%S')} @{username}: {len(fills)} new fills, {len(bets)} bets")
    if not bets:
        return
    if len(bets) == 1:
        b = bets[0]
        notify(f"@{username} placed a bet", describe(b),
               click=f'https://polymarket.com/event/{b["slug"]}' if b["slug"] else None)
    else:
        lines = [describe(b) for b in bets[:8]]
        if len(bets) > 8:
            lines.append(f"…and {len(bets) - 8} more")
        profile = (f"https://polymarket.com/profile/{us['wallet']}" if username.startswith("0x")
                   else f"https://polymarket.com/@{username}")
        notify(f"@{username} placed {len(bets)} bets", "\n".join(lines), click=profile)


def check_all(state, users):
    targets = {name: (wallet, False) for name, wallet in users.items()}
    if LB_TOP > 0:
        try:
            top = fetch_leaderboard()
            if not state.get("lb_armed"):
                notify(f"Following the top {LB_TOP}",
                       f"{LB_PERIOD.title()} {LB_ORDER} leaderboard: " + ", ".join(top))
                state["lb_armed"] = True
            have = {n.lower() for n in targets}
            for name, wallet in top.items():
                if name.lower() not in have:
                    targets[name] = (wallet, True)
        except Exception as e:
            print(f"Leaderboard fetch failed: {e}", file=sys.stderr)

    failures = 0
    for name, (wallet, from_lb) in targets.items():
        us = state["users"].setdefault(name.lower(), {})
        try:
            check_user(name, wallet, us, quiet_start=from_lb)
        except Exception as e:
            failures += 1
            print(f"@{name}: check failed: {e}", file=sys.stderr)

    # forget traders we're no longer following, so the state file stays small
    keep = {n.lower() for n in targets}
    state["users"] = {k: v for k, v in state["users"].items() if k in keep}
    return failures, len(targets)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", action="store_true", help="keep running")
    ap.add_argument("--interval", type=int, default=30, help="seconds between checks")
    args = ap.parse_args()

    users = parse_users(RAW_USERS)
    state = load_state()
    while True:
        failures, total = check_all(state, users)
        save_state(state)
        if not args.loop:
            sys.exit(1 if total == 0 or failures == total else 0)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
