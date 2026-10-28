#!/usr/bin/env python3
"""
Polymarket bet watcher.
Polls a trader's public activity and pushes a phone notification (via ntfy)
whenever they place a new bet. Standard library only, no installs needed.

  python watch.py            # check once (used by GitHub Actions)
  python watch.py --loop     # keep running, check every 30s (run on your own machine)
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
USERNAME    = os.getenv("PM_USERNAME", "homerunhazard")
WALLET      = os.getenv("PM_WALLET", "").strip()          # optional: skip username lookup
NTFY_TOPIC  = os.getenv("NTFY_TOPIC", "").strip()          # your private ntfy topic name
NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
MIN_USD     = float(os.getenv("MIN_USD", "1000"))          # ignore bets smaller than this
BUYS_ONLY   = os.getenv("BUYS_ONLY", "true").lower() == "true"
STATE_FILE  = os.getenv("STATE_FILE", "state.json")
# -----------------------------------------------------------------------------

DATA_API  = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
HEADERS   = {"User-Agent": "pm-bet-watcher/1.0", "Accept": "application/json"}
PAGE      = 500
MAX_PAGES = 6  # up to 3,000 fills per check


def get_json(url, params=None):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def resolve_wallet(username):
    """Turn a Polymarket @username into its proxy wallet address."""
    data = get_json(f"{GAMMA_API}/public-search",
                    {"q": username, "search_profiles": "true"})
    profiles = data.get("profiles") or []
    for p in profiles:
        names = {(p.get("name") or "").lower(), (p.get("pseudonym") or "").lower()}
        if username.lower() in names and p.get("proxyWallet"):
            return p["proxyWallet"]
    found = ", ".join(f'{p.get("name")} ({p.get("proxyWallet")})' for p in profiles[:5])
    sys.exit(f"Couldn't find @{username}. Candidates: {found or 'none'}. "
             "Set PM_WALLET to the 0x address shown on their profile instead.")


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def fill_key(t):
    return f'{t.get("transactionHash")}:{t.get("asset")}:{t.get("side")}:{t.get("size")}'


def fetch_new_fills(wallet, last_ts, seen):
    """Newest-first fills that happened after the last one we processed."""
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
    """One bet is often many fills; merge them by market + outcome + side."""
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


def check_once(state):
    if not state.get("wallet"):
        state["wallet"] = WALLET or resolve_wallet(USERNAME)
        print(f"Watching @{USERNAME} → {state['wallet']}")
    wallet = state["wallet"]
    last_ts, seen = int(state.get("last_ts", 0)), set(state.get("seen", []))

    fills = fetch_new_fills(wallet, last_ts, seen)
    first_run = "last_ts" not in state

    if fills:
        newest = max(int(t.get("timestamp") or 0) for t in fills)
        at_newest = {fill_key(t) for t in fills if int(t.get("timestamp") or 0) == newest}
        state["last_ts"] = newest
        state["seen"] = sorted(at_newest | (seen if newest == last_ts else set()))
    elif first_run:
        state["last_ts"] = int(time.time())
        state["seen"] = []

    if first_run:
        notify(f"Watching @{USERNAME}", "Watcher is live. You'll get a ping on new bets.")
        return  # don't flood you with their history on the first run

    bets = group_bets(fills)
    print(f"{time.strftime('%H:%M:%S')} {len(fills)} new fills, {len(bets)} bets to report")
    if not bets:
        return
    if len(bets) == 1:
        b = bets[0]
        notify(f"@{USERNAME} placed a bet", describe(b),
               click=f'https://polymarket.com/event/{b["slug"]}' if b["slug"] else None)
    else:
        lines = [describe(b) for b in bets[:8]]
        if len(bets) > 8:
            lines.append(f"…and {len(bets) - 8} more")
        notify(f"@{USERNAME} placed {len(bets)} bets", "\n".join(lines),
               click=f"https://polymarket.com/@{USERNAME}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", action="store_true", help="keep running")
    ap.add_argument("--interval", type=int, default=30, help="seconds between checks")
    args = ap.parse_args()

    state = load_state()
    while True:
        try:
            check_once(state)
            save_state(state)
        except Exception as e:
            print(f"Check failed: {e}", file=sys.stderr)
            if not args.loop:
                save_state(state)
                sys.exit(1)
        if not args.loop:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
