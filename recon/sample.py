"""Synthetic one-store, 30-day export set with planted problems, for demos and tests.

Headers mimic the real exports. The numbers are made up, so don't quote them
as Bubbakoo's results.
"""
from __future__ import annotations

import random
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

STORE = "Bubbakoo's Burritos - Sample Store"
NAMES = ["Jane D", "Mike R", "Ana P", "Chris L", "Sam K", "Tony B", "Priya S", "Luis M", "Kate W", "Dev J"]


def generate(days: int = 30, seed: int = 7, end: date | None = None) -> dict[str, pd.DataFrame]:
    rnd = random.Random(seed)
    end = end or date.today() - timedelta(days=1)
    start = end - timedelta(days=days - 1)
    toast, dd, ue, gh = [], [], [], []
    order_no = 1000
    rates = {"doordash": 0.25, "ubereats": 0.25, "grubhub": 0.20}

    for d in range(days):
        day = start + timedelta(days=d)
        for platform, per_day in (("doordash", 22), ("ubereats", 12), ("grubhub", 6)):
            for _ in range(rnd.randint(per_day - 4, per_day + 4)):
                order_no += 1
                t = datetime.combine(day, datetime.min.time()) + timedelta(minutes=rnd.randint(11 * 60, 21 * 60))
                sub = round(rnd.uniform(11, 58), 2)
                tax = round(sub * 0.08875, 2)
                pid = uuid.UUID(int=rnd.getrandbits(128)).hex.upper()
                name = rnd.choice(NAMES)
                roll = rnd.random()
                status, err, desc, mkt = "Delivered", 0.0, "", 0.0
                commission = round(sub * rates[platform], 2)
                in_toast, in_plat, plat_sub = True, True, sub

                if roll < 0.035:                       # error charge
                    err = round(rnd.uniform(4, sub * 0.6), 2)
                    desc = rnd.choice(["Missing item", "Incorrect item", "Order never arrived", "Cold food"])
                elif roll < 0.045:                     # cancelled after prep
                    status, commission, mkt = "Cancelled", 0.0, round(rnd.uniform(1, 3), 2) if rnd.random() < 0.5 else 0.0
                elif roll < 0.050:                     # never hit Toast
                    in_toast = False
                elif roll < 0.054:                     # in Toast, never paid
                    in_plat = False
                elif roll < 0.070 and platform == "ubereats":   # commission drift
                    commission = round(sub * 0.30, 2)
                elif roll < 0.078:                     # stale 3P menu price
                    plat_sub = round(sub - rnd.uniform(0.75, 3.00), 2)

                if in_toast:
                    tab = {"doordash": f"DoorDash - {name} ({pid[:8]})",
                           "ubereats": f"Uber Eats - {name}",
                           "grubhub": f"Grubhub - {name} {order_no}"}[platform]
                    toast.append({
                        "Location": STORE, "Order Id": f"{rnd.getrandbits(40):012d}", "Order #": order_no,
                        "Opened": (t + timedelta(minutes=rnd.randint(0, 3))).strftime("%m/%d/%y %I:%M %p"),
                        "Tab Names": tab, "Revenue Center": "Online", "Dining Options": "Takeout",
                        "Order Source": {"doordash": "DoorDash", "ubereats": "Uber Eats", "grubhub": "Grubhub"}[platform],
                        "Amount": f"{sub:.2f}", "Tax": f"{tax:.2f}", "Total": f"{sub + tax:.2f}", "Voided": "false",
                    })
                if not in_plat:
                    continue
                paid_sub = plat_sub if status != "Cancelled" else 0.0
                proc = round(paid_sub * 0.029 if platform != "doordash" else 0, 2)
                net = round(paid_sub + (tax if paid_sub else 0) - commission - proc - mkt, 2)
                if platform == "doordash":
                    oid = pid[:8] + pid[8:20]
                    dd.append({"Timestamp local time": t.strftime("%Y-%m-%d %H:%M:%S"), "Store name": STORE,
                               "Transaction type": "Order", "DoorDash order ID": oid, "Final order status": status,
                               "Subtotal": paid_sub, "Subtotal tax passed to merchant": tax if paid_sub else 0,
                               "Commission": -commission, "Marketing fees": -mkt, "Error charges": 0, "Adjustments": 0,
                               "Net total": net, "Payout date": (day + timedelta(days=7 - day.weekday())).isoformat(),
                               "Payout ID": f"DDP-{day.isocalendar()[1]}", "Description": ""})
                    if err:
                        dd.append({"Timestamp local time": (t + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S"),
                                   "Store name": STORE, "Transaction type": "Error charge", "DoorDash order ID": oid,
                                   "Final order status": "", "Subtotal": 0, "Subtotal tax passed to merchant": 0,
                                   "Commission": 0, "Marketing fees": 0, "Error charges": 0, "Adjustments": 0,
                                   "Net total": -err, "Payout date": "", "Payout ID": "", "Description": desc})
                elif platform == "ubereats":
                    ue.append({"Store Name": STORE, "Order ID": pid[:8].lower() + "-" + pid[8:12].lower(),
                               "Order Date": t.strftime("%m/%d/%Y"), "Order Accept Time": t.strftime("%m/%d/%Y %H:%M"),
                               "Order Status": "Completed" if status == "Delivered" else "Canceled",
                               "Sales (excl. tax)": paid_sub, "Tax on Sales": tax if paid_sub else 0,
                               "Marketplace Fee": -commission, "Refunds (incl tax)": -err,
                               "Other payments description": desc, "Marketing Adjustment": -mkt,
                               "Total payout ": round(net - err, 2), "Payout Date": (day + timedelta(days=7 - day.weekday())).isoformat()})
                else:
                    base = {"restaurant": STORE, "order_number": str(order_no), "transaction_date": t.strftime("%Y-%m-%d %H:%M"),
                            "order_status": status}
                    gh.append({**base, "transaction_type": "Prepaid Order", "subtotal": paid_sub, "tax": tax if paid_sub else 0,
                               "commission": -round(commission * 0.6, 2), "delivery_commission": -round(commission * 0.4, 2),
                               "processing_fee": -proc, "targeted_promotion": -mkt, "merchant_net_total": net,
                               "deposit_date": (day + timedelta(days=3)).isoformat(), "adjustment_reason": ""})
                    if err:
                        gh.append({**base, "transaction_type": "Prepaid Order Adjustment", "subtotal": 0, "tax": 0,
                                   "commission": 0, "delivery_commission": 0, "processing_fee": 0, "targeted_promotion": 0,
                                   "merchant_net_total": -err, "deposit_date": "", "adjustment_reason": desc})
    return {"toast": pd.DataFrame(toast), "doordash": pd.DataFrame(dd),
            "ubereats": pd.DataFrame(ue), "grubhub": pd.DataFrame(gh)}


def write(folder: str | Path = "sample_data", **kw) -> None:
    folder = Path(folder)
    folder.mkdir(exist_ok=True)
    names = {"toast": "toast_OrderDetails.csv", "doordash": "doordash_transactions.csv",
             "ubereats": "ubereats_payment_details.csv", "grubhub": "grubhub_transactions.csv"}
    for key, df in generate(**kw).items():
        df.to_csv(folder / names[key], index=False)


if __name__ == "__main__":
    write()
