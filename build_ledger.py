#!/usr/bin/env python3
"""
build_ledger.py

Turns copy_ledger.csv (written by copy_watcher.py) into copy_tracker.xlsx,
a single sheet:
  Top:   your unit and fee rate (edit these), plus totals for wagered, fees,
         P&L before fees, net P&L and ROI. Totals follow any filter you apply.
  Table: one row per copied buy or sell: their price, the price when you were
         alerted, YOUR price, slippage, your shares, wager or sale amount,
         fee, result, and P&L.

Your price comes from, in order:
  1. my_fills.csv, if you logged what you actually got  (columns: bet,your_price,your_amount)
  2. the market price when the alert was sent
  3. the trader's price

P&L per row:
  buy  = shares x price now/final - wagered
  sell = sold for - shares x price now/final  (gain or loss vs. holding)
These add up to your true P&L per position.

Fees use Polymarket's taker formula: fee = rate x shares x price x (1 - price),
charged on buys and sells (not on payouts). Default rate 0.06 = Polymarket US.

Needs: pip install openpyxl
"""

import argparse
import csv
import time
from datetime import datetime

from openpyxl import Workbook
from openpyxl.comments import Comment
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from low_trade_leaders import fetch_markets

FONT = "Arial"
HDR_FILL = PatternFill("solid", start_color="1F3864")
INPUT_FILL = PatternFill("solid", start_color="FFFF00")
TOTAL_FILL = PatternFill("solid", start_color="D9E1F2")
THIN = Side(style="thin", color="BFBFBF")
USD = '$#,##0.00;($#,##0.00);"-"'
PRICE = '$0.000'
PCT = '0.0%;(0.0%);"-"'
SHARES = '#,##0.00;(#,##0.00);"-"'


def style_header(ws, row, ncols):
    for c in range(1, ncols + 1):
        cell = ws.cell(row=row, column=c)
        cell.font = Font(name=FONT, bold=True, color="FFFFFF")
        cell.fill = HDR_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = Border(bottom=THIN)
    ws.row_dimensions[row].height = 32


def set_font(ws):
    for row in ws.iter_rows():
        for cell in row:
            if cell.font is None or cell.font.name != FONT:
                f = cell.font
                cell.font = Font(name=FONT, bold=f.bold, color=f.color, italic=f.italic, size=f.size)


def widths(ws, w):
    for i, v in enumerate(w, 1):
        ws.column_dimensions[get_column_letter(i)].width = v


def market_status(cid, outcome_idx, markets):
    m = markets.get(cid)
    if not m or not m["prices"]:
        return None, "No price"
    try:
        price = m["prices"][outcome_idx]
    except (IndexError, TypeError):
        return None, "No price"
    if m["closed"]:
        if price >= 0.99:
            return price, "Won"
        if price <= 0.01:
            return price, "Lost"
        return price, "Settled"
    return price, "Open"


def to_price(v):
    """Accepts 0.45, 45, 45c or 45¢."""
    v = str(v or "").strip().lower().rstrip("c¢").strip()
    if not v:
        return None
    x = float(v)
    return x / 100 if x > 1 else x


def read_fills(path):
    out = {}
    try:
        with open(path, newline="") as f:
            for r in csv.DictReader(f):
                try:
                    bet = int(str(r.get("bet") or "").strip().lstrip("#"))
                except ValueError:
                    continue
                price = to_price(r.get("your_price"))
                amt = str(r.get("your_amount") or "").strip().lstrip("$")
                out[bet] = (price, float(amt) if amt else None)
    except FileNotFoundError:
        pass
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--ledger", default="copy_ledger.csv")
    p.add_argument("--out", default="copy_tracker.xlsx")
    p.add_argument("--unit", type=float, default=10.0)
    p.add_argument("--fee-rate", type=float, default=0.06)
    p.add_argument("--fills", default="my_fills.csv")
    a = p.parse_args()

    try:
        with open(a.ledger, newline="") as f:
            bets = list(csv.DictReader(f))
    except FileNotFoundError:
        bets = []
    for n, b in enumerate(bets, 1):  # bet # = row number in the ledger (matches the alert)
        b["id"] = n
    bets.sort(key=lambda r: int(r["timestamp"] or 0))
    fills = read_fills(a.fills)

    markets = fetch_markets([b["condition_id"] for b in bets]) if bets else {}

    wb = Workbook()
    ws = wb.active
    ws.title = "Tracker"

    # ---------- top block: inputs + totals ----------
    ws["A1"] = "Copy-Trade Tracker"
    ws["A1"].font = Font(name=FONT, bold=True, size=14)
    ws["D1"] = f"Refreshed {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}"
    ws["D1"].font = Font(name=FONT, italic=True, color="808080")
    labels = ["Your unit", "Fee rate", "Wagered", "Fees", "Slippage cost", "P&L before fees",
              "Net P&L", "ROI after fees"]
    for c, h in enumerate(labels, 1):
        ws.cell(row=2, column=c, value=h)
    style_header(ws, 2, len(labels))
    first, last = 6, max(len(bets) + 5, 6)
    rng = lambda col: f"{col}{first}:{col}{last}"
    ws["A3"], ws["B3"] = a.unit, a.fee_rate
    ws["C3"] = f"=SUBTOTAL(9,{rng('Q')})"
    ws["D3"] = f"=SUBTOTAL(9,{rng('S')})"
    ws["E3"] = f"=SUBTOTAL(9,{rng('T')})"
    ws["F3"] = f"=SUBTOTAL(9,{rng('W')})"
    ws["G3"] = f"=SUBTOTAL(9,{rng('X')})"
    ws["H3"] = "=IF(C3=0,0,G3/C3)"
    for c in ("A3", "B3"):
        ws[c].font = Font(name=FONT, color="0000FF")
        ws[c].fill = INPUT_FILL
    ws["A3"].number_format = "$#,##0.00"
    ws["B3"].number_format = "0.000"
    for c in ("C3", "D3", "E3", "F3", "G3"):
        ws[c].number_format = USD
        ws[c].font = Font(name=FONT, bold=True)
    ws["H3"].number_format = PCT
    ws["H3"].font = Font(name=FONT, bold=True)
    ws["E3"].comment = Comment("Extra you paid (or gave up on sells) versus the trader's price. "
                               "Already included in P&L.", "tracker")
    ws["A3"].comment = Comment("Your unit. Each copied buy = unit x (their bet / their unit).", "tracker")
    ws["B3"].comment = Comment(
        "Taker fee = rate x shares x price x (1 - price), on buys and sells, not payouts. "
        "0.06 = Polymarket US schedule effective July 1, 2026 (max $1.50 per 100 shares at 50c). "
        "Global Polymarket varies by category, roughly 0.03-0.07; geopolitics is 0.", "tracker")
    ws["A4"] = ("Yellow cells are editable. Log your real fills in my_fills.csv (bet,your_price,your_amount); "
                "otherwise your price = price when alerted. Totals follow any filter. "
                "Open bets use the price at last refresh; Won = $1/share, Lost = $0.")
    ws["A4"].font = Font(name=FONT, italic=True, color="808080", size=9)

    # ---------- bet table ----------
    hdr = ["Bet #", "Time", "Trader", "Market", "Outcome", "Side", "Their price", "Price at alert",
           "Your price", "Your price from", "Slippage per share", "Their $", "Their unit",
           "Their sell %", "Your actual $", "Your shares", "Wagered", "Sold for", "Fee",
           "Slippage $", "Status", "Price now / final", "P&L before fees", "Net P&L", "Link", "Key"]
    for c, h in enumerate(hdr, 1):
        ws.cell(row=first - 1, column=c, value=h)
    style_header(ws, first - 1, len(hdr))
    ws.cell(row=first - 1, column=11).comment = Comment(
        "Positive = you got a worse price than the trader (paid more on a buy, got less on a sell).", "tracker")

    for i, b in enumerate(bets, first):
        m = markets.get(b["condition_id"])
        idx = m["tokens"].index(b["asset"]) if m and b["asset"] in m["tokens"] else None
        price_now, status = market_status(b["condition_id"], idx, markets)
        if price_now is None:  # fall back to the last traded price we logged
            price_now = float([x for x in bets if x["asset"] == b["asset"]][-1]["price"] or 0)
        their_price = float(b["price"] or 0)
        alert_price = to_price(b.get("alert_price"))
        actual_price, actual_amt = fills.get(b["id"], (None, None))
        if actual_price:
            your_price, source = actual_price, "Your fill"
        elif alert_price:
            your_price, source = alert_price, "At alert"
        else:
            your_price, source = their_price, "Their price"
        sell_pct = b["their_sell_pct"]
        row = {
            "A": b["id"], "B": b["time"], "C": b["trader"], "D": b["market"], "E": b["outcome"],
            "F": b["side"], "G": their_price, "H": alert_price, "I": your_price, "J": source,
            "L": float(b["their_usd"] or 0), "M": float(b["their_unit"] or 0),
            "N": float(sell_pct) if sell_pct not in ("", None) else None,
            "O": actual_amt if b["side"] == "BUY" else None,
            "U": status, "V": price_now, "Y": b["link"], "Z": b["asset"],
        }
        for col, v in row.items():
            ws[f"{col}{i}"] = v
        ws[f"K{i}"] = f'=IF(F{i}="BUY",I{i}-G{i},G{i}-I{i})'
        if b["side"] == "BUY":  # your actual $ if logged, else unit x their units
            ws[f"P{i}"] = f'=IFERROR(IF(O{i}<>"",O{i},$A$3*L{i}/M{i})/I{i},0)'
        elif i == first:
            ws[f"P{i}"] = 0
        else:  # sell the same % of your shares in this outcome
            ws[f"P{i}"] = f"=-N{i}*SUMIFS(P${first}:P{i-1},Z${first}:Z{i-1},Z{i})"
        ws[f"Q{i}"] = f'=IF(F{i}="BUY",P{i}*I{i},0)'
        ws[f"R{i}"] = f'=IF(F{i}="SELL",ABS(P{i})*I{i},0)'
        ws[f"S{i}"] = f"=$B$3*ABS(P{i})*I{i}*(1-I{i})"
        ws[f"T{i}"] = f"=ABS(P{i})*K{i}"
        ws[f"W{i}"] = f'=IF(F{i}="BUY",P{i}*V{i}-Q{i},R{i}-ABS(P{i})*V{i})'
        ws[f"X{i}"] = f"=W{i}-S{i}"
        for col, fmt in (("G", PRICE), ("H", PRICE), ("I", PRICE), ("K", '+$0.000;-$0.000;"-"'),
                         ("L", USD), ("M", USD), ("N", PCT), ("O", USD), ("P", SHARES), ("Q", USD),
                         ("R", USD), ("S", USD), ("T", USD), ("V", PRICE), ("W", USD), ("X", USD)):
            ws[f"{col}{i}"].number_format = fmt
        for col in "GHLMNV":
            ws[f"{col}{i}"].font = Font(name=FONT, color="0000FF")
        ws[f"I{i}"].font = Font(name=FONT, color="0000FF", bold=True)
        if source == "Your fill":
            ws[f"I{i}"].fill = PatternFill("solid", start_color="E2EFDA")

    if not bets:
        ws[f"A{first}"] = "No copied bets yet. They'll appear after the watcher sends its first alert."
    widths(ws, [7, 22, 16, 40, 14, 7, 9, 9, 9, 11, 10, 11, 10, 9, 10, 11, 10, 10, 8, 10, 8, 10, 12, 11, 30, 10])
    ws.column_dimensions["Z"].hidden = True
    ws.freeze_panes = f"A{first}"
    if bets:
        ws.auto_filter.ref = f"A{first - 1}:Y{last}"
    set_font(ws)
    wb.save(a.out)
    print(f"Wrote {a.out}: {len(bets)} bets")


if __name__ == "__main__":
    main()

