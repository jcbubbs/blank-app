"""Match Toast orders to platform orders and flag money we can recover."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

import pandas as pd

# [ASSUMPTION] Defaults. The app lets users override them, and each franchisee
# should replace them with the rates in their own platform contracts.
DEFAULT_RATES = {"doordash": 0.25, "ubereats": 0.25, "grubhub": 0.20}
# Uber's merchant terms say 14 days even though its help center says 30.
# Grubhub's help center says 30. DoorDash is set to 14 as a safe default.
DEFAULT_WINDOWS = {"doordash": 14, "ubereats": 14, "grubhub": 30}

RULES = {
    "R1": "Error charge / refund taken from payout",
    "R2": "Cancelled by platform after food was made",
    "R3": "Platform order not found in Toast",
    "R4": "Toast 3P order missing from platform report",
    "R5": "Commission above contracted rate",
    "R6": "Marketing fee on cancelled order",
    "R7": "Platform paid on lower subtotal than Toast",
}
# Rules whose dollars can be clawed back via a platform dispute or ticket.
RECOVERABLE_RULES = {"R1", "R2", "R4", "R5", "R6", "R7"}


@dataclass
class Settings:
    rates: dict = field(default_factory=lambda: dict(DEFAULT_RATES))
    windows: dict = field(default_factory=lambda: dict(DEFAULT_WINDOWS))
    rate_tolerance: float = 0.005     # 0.5 percentage points
    time_window_min: int = 20
    amount_tol: float = 0.05
    subtotal_gap: float = 0.50
    loose_pct: float = 0.15
    win_rate: float = 0.60
    as_of: date = field(default_factory=date.today)


def _key(order_id: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(order_id).upper())


def match_orders(toast: pd.DataFrame, plat: pd.DataFrame, s: Settings) -> pd.DataFrame:
    """Attach toast_order_id + match_tier to each platform order (greedy 1:1).

    Tier 1: the platform order ID (or its first 8 characters, i.e. the tablet
            short code) appears in the Toast tab name or order number.
    Tier 2: same platform, opened within time_window_min, subtotal within
            amount_tol. Toast orders not tagged to any platform are allowed
            only as low-confidence matches.
    """
    plat = plat.copy()
    plat["toast_order_id"] = None
    plat["match_tier"] = None
    if toast.empty or plat.empty:
        return plat
    used: set[str] = set()
    blobs = dict(zip(toast["toast_order_id"], toast["search_blob"]))

    for i, row in plat.iterrows():
        k = _key(row["order_id"])
        needles = {n for n in (k, k[:8], _key(row.get("pos_order_id", ""))) if len(n) >= 6}
        for tid, blob in blobs.items():
            if tid not in used and any(n in blob.replace(" ", "") for n in needles):
                plat.at[i, "toast_order_id"], plat.at[i, "match_tier"] = tid, "T1 exact"
                used.add(tid)
                break

    window = pd.Timedelta(minutes=s.time_window_min)
    for i, row in plat[plat["toast_order_id"].isna()].iterrows():
        if pd.isna(row["order_time"]) or row["subtotal"] <= 0:
            continue
        cand = toast[
            ~toast["toast_order_id"].isin(used)
            & toast["platform"].isin([row["platform"], ""])
            & ((toast["opened"] - row["order_time"]).abs() <= window)
            & ((toast["subtotal"] - row["subtotal"]).abs() <= s.amount_tol)
        ]
        if cand.empty:
            continue
        cand = cand.assign(
            tagged=cand["platform"] == row["platform"],
            dt=(cand["opened"] - row["order_time"]).abs(),
        ).sort_values(["tagged", "dt"], ascending=[False, True])
        best = cand.iloc[0]
        plat.at[i, "toast_order_id"] = best["toast_order_id"]
        plat.at[i, "match_tier"] = "T2 time+amount" if best["tagged"] else "T2 time+amount (untagged)"
        used.add(best["toast_order_id"])

    # Tier 3 (review): same platform tag, and either the amount is within
    # loose_pct (a stale 3P menu price) or the platform zeroed the subtotal
    # (a cancellation) and the times are within 5 min. Without this pass,
    # price-drift and cancel cases would show up as unmatched pairs.
    for i, row in plat[plat["toast_order_id"].isna()].iterrows():
        if pd.isna(row["order_time"]):
            continue
        cand = toast[
            ~toast["toast_order_id"].isin(used)
            & (toast["platform"] == row["platform"])
            & ((toast["opened"] - row["order_time"]).abs() <= window)
        ]
        dt = (cand["opened"] - row["order_time"]).abs()
        if row["subtotal"] > 0:
            cand = cand[((cand["subtotal"] - row["subtotal"]).abs() / cand["subtotal"].clip(lower=0.01)) <= s.loose_pct]
        else:
            cand = cand[dt <= pd.Timedelta(minutes=5)]
        if cand.empty:
            continue
        best = cand.loc[(cand["opened"] - row["order_time"]).abs().idxmin()]
        plat.at[i, "toast_order_id"], plat.at[i, "match_tier"] = best["toast_order_id"], "T3 loose (review)"
        used.add(best["toast_order_id"])
    return plat


_STOP = {"BUILD", "YOUR", "OWN", "MISSING", "INCORRECT", "QUALITY", "FOOD", "SIGNATURE", "LARGE",
         "SMALL", "MEAL", "DEAL", "THE", "AND", "WITH", "ENTIRELY", "WRONG", "ORDER"}


def _tokens(text: str) -> set[str]:
    words = re.findall(r"[A-Z]{4,}", str(text).upper())
    return {w[:-1] if w.endswith("S") and len(w) > 4 else w for w in words} - _STOP


def item_evidence(claim: str, ticket: pd.DataFrame) -> tuple[str, str]:
    """Compare a platform's error description with the Toast ticket's items.

    Returns (evidence, confidence). Finding the claimed item on the ticket
    proves it was rung and sent to the kitchen, not that it went in the bag.
    It's still the proof DoorDash asks for.
    """
    if ticket.empty:
        return "No Toast item data for this order", ""
    claims = [c for c in re.split(r"[;|]", str(claim)) if c.strip()]
    wanted = set().union(*(_tokens(c) for c in claims)) if claims else set()
    if not wanted:
        return f"Whole-order claim. Toast ticket had {len(ticket)} items, all sent to kitchen", "medium"
    hits = ticket[ticket["item"].map(lambda x: bool(wanted & _tokens(x)))]
    if hits.empty:
        return "Claimed item NOT on the Toast ticket, so the charge is likely valid. Skip the dispute and coach the store", "low"
    live = hits[~hits["voided"]]
    if live.empty:
        return "Claimed item was VOIDED on the Toast ticket, so the charge is likely valid", "low"
    sent = live["sent"].min()
    names = ", ".join(sorted(live["item"].unique())[:3])
    when = f" at {sent:%I:%M %p}" if pd.notna(sent) else ""
    return f"Toast ticket shows {names} rung and sent to kitchen{when}, not voided", "high"


# How each rule gets filed. Error charges have a Dispute button in the portal;
# everything else needs a support ticket or the account manager.
FILE_VIA = {
    "R1": "Portal: Dispute charge", "R2": "Support ticket", "R3": "Store ops (no dispute)",
    "R4": "Support ticket", "R5": "Account manager / support", "R6": "Support ticket", "R7": "Menu fix + support ticket",
}

FILING_STEPS = {
    "doordash": "DoorDash Merchant Portal → Financials → Transactions → filter type = Error charge → open the order → Dispute Charge → paste the text.",
    "ubereats": "Uber Eats Manager → Orders (or Payments) → open the order → Dispute adjustment → paste the text and attach a ticket photo if you have one.",
    "grubhub": "restaurant.grubhub.com → Financials → Transactions → filter Prepaid Order Adjustment → Dispute → paste the text.",
}
PLATFORM_NAMES = {"doordash": "DoorDash", "ubereats": "Uber Eats", "grubhub": "Grubhub"}


def _finding(row, rule, amount, evidence, s: Settings, toast_row=None, confidence="medium", text=""):
    when = row.get("order_time") if row is not None else None
    if (when is None or pd.isna(when)) and toast_row is not None:
        when = toast_row["opened"]
    platform = row["platform"] if row is not None else toast_row["platform"]
    deadline = (pd.Timestamp(when) + pd.Timedelta(days=s.windows.get(platform, 14))).date() if pd.notna(when) else None
    days_left = (deadline - s.as_of).days if deadline else None
    return {
        "platform": platform,
        "platform_order_id": row["order_id"] if row is not None else "",
        "toast_order": str(toast_row["order_number"] or toast_row["toast_order_id"]) if toast_row is not None else "",
        "order_time": when,
        "rule": rule,
        "issue": RULES[rule],
        "amount": round(float(amount), 2),
        "confidence": confidence,
        "evidence": evidence,
        "dispute_deadline": deadline,
        "days_left": days_left,
        "status": "expired" if days_left is not None and days_left < 0 else "open",
        "file_via": FILE_VIA[rule],
        "dispute_text": text,
    }


def find_issues(toast: pd.DataFrame, matched: pd.DataFrame, s: Settings, items: pd.DataFrame | None = None) -> pd.DataFrame:
    have_toast = not toast.empty
    tickets = items.groupby("order_number") if items is not None and not items.empty else None
    by_id = toast.set_index("toast_order_id") if have_toast else pd.DataFrame()
    out = []
    for _, r in matched.iterrows():
        t = by_id.loc[r["toast_order_id"]] if r["toast_order_id"] is not None else None
        if t is not None:
            t = t.copy()
            t["toast_order_id"] = r["toast_order_id"]
        rung = t is not None and not t["voided"]
        cancelled = "cancel" in str(r["status"]).lower()

        if r["error_charge"] < 0:
            charged, credited = -r["error_charge"], max(0.0, r["adjustment"])
            ev = f"{r['platform']} took ${charged:.2f}"
            if r["description"]:
                ev += f" ({r['description']})"
            if r["subtotal"] > 0:
                ev += f" on a ${r['subtotal']:.2f} order ({charged / r['subtotal']:.0%})"
            if credited:
                ev += f"; ${credited:.2f} already credited back"
            confidence = "high" if rung else "medium"
            proof = ""
            if rung:
                ev += f". Toast #{t['order_number']} was rung at ${t['subtotal']:.2f}"
                proof = f"Our POS (Toast order #{t['order_number']}) shows this order was rung in full at ${t['subtotal']:.2f}."
                if tickets is not None:
                    num = str(t["order_number"])
                    ticket = tickets.get_group(num) if num in tickets.groups else pd.DataFrame(columns=items.columns)
                    item_ev, item_conf = item_evidence(r["description"], ticket)
                    ev += f". {item_ev}"
                    confidence = item_conf or "medium"
                    if item_conf == "high":
                        proof = f"Our POS (Toast order #{t['order_number']}) shows {item_ev.removeprefix('Toast ticket shows ')}."
                    elif item_conf == "medium":
                        proof = f"Our POS (Toast order #{t['order_number']}) shows all {len(ticket)} items were rung and sent to the kitchen, and the order was handed to the courier."
            claim = r["description"] or "error charge"
            text = (f"Disputing the ${charged - credited:.2f} error charge on order {r['order_id']} "
                    f"({pd.Timestamp(r['order_time']):%m/%d/%y %I:%M %p}). Claim: {claim}. "
                    f"{proof or 'The order was prepared as placed.'} "
                    f"Please reverse this charge.")
            f = _finding(r, "R1", max(0.0, charged - credited), ev, s, t, confidence, text)
            if credited >= charged - 0.01:
                f["status"] = "credited"
            elif confidence == "low":
                f["status"] = "likely valid"   # don't dispute; losing disputes gets DD self-service restricted
            out.append(f)

        if cancelled and r["net_payout"] <= 0.01 and rung:
            out.append(_finding(r, "R2", t["subtotal"],
                                f"Platform cancelled with ${r['net_payout']:.2f} paid; Toast #{t['order_number']} was made (${t['subtotal']:.2f}), not voided",
                                s, t, "high",
                                f"Order {r['order_id']} ({pd.Timestamp(r['order_time']):%m/%d/%y %I:%M %p}) was cancelled ({r['status']}) after our kitchen "
                                f"prepared it. Toast order #{t['order_number']} shows ${t['subtotal']:.2f} of food made and not voided, and we were paid "
                                f"${r['net_payout']:.2f}. Please compensate the ${t['subtotal']:.2f} food cost."))

        if have_toast and t is None and r["subtotal"] > 0 and not cancelled:
            out.append(_finding(r, "R3", 0,
                                f"${r['subtotal']:.2f} order on {r['platform']} with no Toast match. Check tablet/injection (inventory and tax impact)",
                                s, None, "info"))

        rate = s.rates.get(r["platform"])
        # Commission is charged on the subtotal after discounts the store funds.
        base = r["subtotal"] - r["promo"]
        if rate and base > 0 and not cancelled:
            expected = base * rate
            over = r["commission"] - expected
            if over > max(0.10, base * s.rate_tolerance):
                out.append(_finding(r, "R5", over,
                                    f"Commission ${r['commission']:.2f} = {r['commission'] / base:.1%} of ${base:.2f} (after store-funded promo) vs contracted {rate:.0%} (${expected:.2f})",
                                    s, t, "medium",
                                    f"Order {r['order_id']}: commission was ${r['commission']:.2f} ({r['commission'] / base:.1%} of the ${base:.2f} "
                                    f"commissionable subtotal), but our contracted rate is {rate:.0%} (${expected:.2f}). Please refund the ${over:.2f} difference "
                                    f"and confirm the rate on our account."))

        if cancelled and r["marketing_fee"] > 0:
            out.append(_finding(r, "R6", r["marketing_fee"],
                                f"Marketing fee ${r['marketing_fee']:.2f} charged on a cancelled order", s, t, "medium",
                                f"Order {r['order_id']} was cancelled ({r['status']}) but we were charged a ${r['marketing_fee']:.2f} marketing fee. "
                                f"No sale happened, so please refund the fee."))

        if rung and r["subtotal"] > 0 and t["subtotal"] - r["subtotal"] > s.subtotal_gap:
            gap = t["subtotal"] - r["subtotal"]
            out.append(_finding(r, "R7", gap,
                                f"Toast subtotal ${t['subtotal']:.2f} vs {r['platform']} ${r['subtotal']:.2f}: menu price drift or item missing from the platform ticket",
                                s, t, "low",
                                f"Order {r['order_id']}: we were paid on a ${r['subtotal']:.2f} subtotal, but the ticket sent to our POS totals "
                                f"${t['subtotal']:.2f}. Please review and pay the ${gap:.2f} difference. (Store: check the menu prices on this platform.)"))

    matched_ids = set(matched["toast_order_id"].dropna())
    orphans = toast.iloc[0:0] if not have_toast else toast[(toast["platform"] != "") & ~toast["voided"] & ~toast["toast_order_id"].isin(matched_ids)]
    for _, t in orphans.iterrows():
        out.append(_finding(None, "R4", t["subtotal"],
                            f"Toast shows a {t['platform']} order (#{t['order_number']}, ${t['subtotal']:.2f}) with no platform record. Confirm it was paid",
                            s, t, "medium",
                            f"Our POS received a {PLATFORM_NAMES.get(t['platform'], t['platform'])} order on {pd.Timestamp(t['opened']):%m/%d/%y %I:%M %p} "
                            f"(Toast #{t['order_number']}, ${t['subtotal']:.2f}) that isn't in our payout report. "
                            f"Please confirm it was paid, or pay it."))

    cols = ["platform", "platform_order_id", "toast_order", "order_time", "rule", "issue", "amount",
            "confidence", "evidence", "dispute_deadline", "days_left", "status", "file_via", "dispute_text"]
    df = pd.DataFrame(out, columns=cols)
    return df.sort_values(["status", "days_left", "amount"], ascending=[False, True, False], na_position="last").reset_index(drop=True)


def rate_mix(matched: pd.DataFrame) -> pd.DataFrame:
    """Weekly spread of effective commission rates for each platform.

    A plan change shows up as one rate bucket disappearing and another
    appearing. That's the kind of shift a per-order rule can't see.
    """
    m = matched[~matched["status"].astype(str).str.lower().str.contains("cancel")].copy()
    m["base"] = m["subtotal"] - m["promo"]
    m = m[(m["base"] > 0) & m["order_time"].notna()]
    if m.empty:
        return pd.DataFrame()
    m["rate"] = (m["commission"] / m["base"] * 100).round(0).astype(int).astype(str) + "%"
    m["week"] = m["order_time"].dt.to_period("W").dt.start_time.dt.date
    return (m.pivot_table(index=["platform", "week"], columns="rate", values="order_id", aggfunc="count", fill_value=0)
             .reset_index())


def summarize(toast: pd.DataFrame, matched: pd.DataFrame, issues: pd.DataFrame, s: Settings) -> dict:
    live = matched[~matched["status"].astype(str).str.lower().str.contains("cancel")]
    times = pd.concat([matched["order_time"], toast["opened"]]).dropna()
    days = max(1, (times.max() - times.min()).days + 1) if not times.empty else 1
    rec = issues[issues["rule"].isin(RECOVERABLE_RULES)]
    open_amt = rec.loc[rec["status"] == "open", "amount"].sum()
    expired_amt = rec.loc[rec["status"] == "expired", "amount"].sum()
    expected = open_amt * s.win_rate
    sales = live["subtotal"].sum()
    return {
        "days": days,
        "orders": len(live),
        "sales": sales,
        "match_rate": matched["toast_order_id"].notna().mean() if len(matched) else 0.0,
        "error_charges": -matched["error_charge"].sum(),
        "fees": matched[["commission", "processing_fee", "marketing_fee"]].sum().sum(),
        "flagged": rec["amount"].sum(),
        "open": open_amt,
        "expired": expired_amt,
        "expected_recovery": expected,
        "annualized": (open_amt + expired_amt) * s.win_rate / days * 365,
        "pct_of_sales": (rec["amount"].sum() / sales) if sales else 0.0,
    }


def run(toast: pd.DataFrame, platforms: list[pd.DataFrame], s: Settings, items: pd.DataFrame | None = None):
    plat = pd.concat([p for p in platforms if not p.empty], ignore_index=True) if any(not p.empty for p in platforms) else pd.DataFrame()
    if plat.empty:
        raise ValueError("No platform orders loaded. Upload at least one DoorDash, Uber Eats or Grubhub CSV.")
    matched = match_orders(toast, plat, s)
    issues = find_issues(toast, matched, s, items)
    return matched, issues, summarize(toast, matched, issues, s)
