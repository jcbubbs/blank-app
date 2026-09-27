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
    "R8": "Cancelled unpaid, but order reached Toast",
    "R9": "Store-caused cancellation (food lost)",
}
# Rules whose dollars can be clawed back via a platform dispute or ticket.
RECOVERABLE_RULES = {"R1", "R2", "R4", "R5", "R6", "R7", "R8"}
# DoorDash reasons that blame the store. We can't dispute these, but they
# still cost food and labor, so they're reported for coaching.
STORE_FAULT = re.compile(r"wrong order handed|staff requested|avoidable store", re.I)


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
            & toast["platform"].isin([row["platform"], "3p", ""])
            & ((toast["opened"] - row["order_time"]).abs() <= window)
            & ((toast["subtotal"] - row["subtotal"]).abs() <= s.amount_tol)
        ]
        if cand.empty:
            continue
        cand = cand.assign(
            tagged=cand["platform"].isin([row["platform"], "3p"]),
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
            & toast["platform"].isin([row["platform"], "3p"])
            & ((toast["opened"] - row["order_time"]).abs() <= window)
        ]
        dt = (cand["opened"] - row["order_time"]).abs()
        if row["subtotal"] > 0:
            close_amt = ((cand["subtotal"] - row["subtotal"]).abs() / cand["subtotal"].clip(lower=0.01)) <= s.loose_pct
            # Rebuilt-from-items totals leave out paid modifiers, so they can
            # only come in under the platform subtotal. Require the times to be
            # tight instead.
            partial = cand["amount_partial"] & (cand["subtotal"] <= row["subtotal"] + s.amount_tol) & (dt <= pd.Timedelta(minutes=3))
            cand = cand[close_amt | partial]
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
    "R8": "Support ticket", "R9": "Store ops (no dispute)",
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


# Toast can prove an item was rung, but not which sauce went on it or whether
# the bag had its chips. For these categories the proof is weaker.
WEAK_PROOF_CATEGORIES = ("ingredient", "side item", "quality", "temperature", "quantity", "size", "incorrect", "wrong")


def find_issues(toast: pd.DataFrame, matched: pd.DataFrame, s: Settings, items: pd.DataFrame | None = None,
                claims: pd.DataFrame | None = None, cancels: pd.DataFrame | None = None) -> pd.DataFrame:
    have_toast = not toast.empty
    claim_by_order = claims.set_index("order_id").to_dict("index") if claims is not None and not claims.empty else {}
    dasher_orders = {}
    for oid, c in claim_by_order.items():
        for d in filter(None, str(c["dasher"]).split("; ")):
            dasher_orders.setdefault(d, set()).add(oid)
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
            ops = claim_by_order.get(str(r["order_id"]).upper())
            link = ""
            if ops:
                link = ops["order_link"]
                category = ops["error_category"]
                ev += f". DoorDash category: {category}"
                if ops["customer_comment"]:
                    ev += f'. Customer said: "{ops["customer_comment"]}"'
                repeat = [d for d in str(ops["dasher"]).split("; ") if len(dasher_orders.get(d, ())) > 1]
                if repeat:
                    ev += f". Dasher {', '.join(repeat)} delivered {max(len(dasher_orders[d]) for d in repeat)} claimed orders this period"
                if confidence == "high" and any(w in f"{category} {r['description']}".lower() for w in WEAK_PROOF_CATEGORIES):
                    confidence = "medium"
                    ev += ". Toast proves the item was rung, not its ingredients, portions, sides or which bag it went in, so the proof is weaker"
                claim = f"{category}: {ops['claimed_items']}" if ops["claimed_items"] else claim
            elif confidence == "high" and any(w in str(r["description"]).lower() for w in WEAK_PROOF_CATEGORIES):
                confidence = "medium"
                ev += ". Quality/ingredient claim: Toast proves the item was rung, not how it was made"
            text = (f"Disputing the ${charged - credited:.2f} error charge on order {r['order_id']} "
                    f"({pd.Timestamp(r['order_time']):%m/%d/%y %I:%M %p}). Claim: {claim}. "
                    f"{proof or 'The order was prepared as placed.'} "
                    f"Please reverse this charge.")
            f = _finding(r, "R1", max(0.0, charged - credited), ev, s, t, confidence, text)
            f["order_link"] = link
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

        if rung and not t.get("amount_partial", False) and r["subtotal"] > 0 and t["subtotal"] - r["subtotal"] > s.subtotal_gap:
            gap = t["subtotal"] - r["subtotal"]
            out.append(_finding(r, "R7", gap,
                                f"Toast subtotal ${t['subtotal']:.2f} vs {r['platform']} ${r['subtotal']:.2f}: menu price drift or item missing from the platform ticket",
                                s, t, "low",
                                f"Order {r['order_id']}: we were paid on a ${r['subtotal']:.2f} subtotal, but the ticket sent to our POS totals "
                                f"${t['subtotal']:.2f}. Please review and pay the ${gap:.2f} difference. (Store: check the menu prices on this platform.)"))

    matched_ids = set(matched["toast_order_id"].dropna())
    orphans = toast.iloc[0:0] if not have_toast else toast[~toast["platform"].isin(["", "3p"]) & ~toast["voided"] & ~toast["toast_order_id"].isin(matched_ids)]
    for _, t in orphans.iterrows():
        out.append(_finding(None, "R4", t["subtotal"],
                            f"Toast shows a {t['platform']} order (#{t['order_number']}, ${t['subtotal']:.2f}) with no platform record. Confirm it was paid",
                            s, t, "medium",
                            f"Our POS received a {PLATFORM_NAMES.get(t['platform'], t['platform'])} order on {pd.Timestamp(t['opened']):%m/%d/%y %I:%M %p} "
                            f"(Toast #{t['order_number']}, ${t['subtotal']:.2f}) that isn't in our payout report. "
                            f"Please confirm it was paid, or pay it."))

    # Unpaid cancellations from the Operations Quality file. Most never show up
    # in the financial report, so check them against Toast directly.
    if cancels is not None and not cancels.empty:
        used = set(matched["toast_order_id"].dropna())
        for _, c in cancels[~cancels["paid"]].iterrows():
            t = None
            if have_toast and pd.notna(c["placed"]):
                cand = toast[~toast["toast_order_id"].isin(used)
                             & ((toast["opened"] - c["placed"]).abs() <= pd.Timedelta(minutes=10))
                             & ((toast["subtotal"] - c["subtotal"]).abs() <= s.amount_tol) & ~toast["voided"]]
                if not cand.empty:
                    t = cand.loc[(cand["opened"] - c["placed"]).abs().idxmin()]
                    used.add(t["toast_order_id"])
            row = {"platform": "doordash", "order_id": c["order_id"], "order_time": c["placed"]}
            why = f"{c['category']}: {c['reason']}".strip(": ")
            if STORE_FAULT.search(why):
                out.append(_finding(row, "R9", c["subtotal"],
                                    f"DoorDash cancelled and didn't pay ({why}). ${c['subtotal']:.2f} of food"
                                    + (f" rung on Toast #{t['order_number']}" if t is not None else "") + ". Coach the store",
                                    s, t, "info"))
            elif t is not None:
                out.append(_finding(row, "R8", c["subtotal"],
                                    f"DoorDash cancelled and paid $0 ({why}), but Toast #{t['order_number']} received and rang it at ${t['subtotal']:.2f}",
                                    s, t, "high" if "confirm" in why.lower() else "medium",
                                    f"Order {c['order_id']} ({c['placed']:%m/%d/%y %I:%M %p}) was cancelled with reason \"{c['reason']}\" and we were paid $0. "
                                    f"The order did reach our POS (Toast #{t['order_number']}, ${t['subtotal']:.2f}). "
                                    f"Please pay the ${t['subtotal']:.2f} subtotal."))

    cols = ["platform", "platform_order_id", "toast_order", "order_time", "rule", "issue", "amount",
            "confidence", "evidence", "dispute_deadline", "days_left", "status", "file_via", "dispute_text", "order_link"]
    df = pd.DataFrame(out, columns=cols)
    df["order_link"] = df["order_link"].fillna("")
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


def run(toast: pd.DataFrame, platforms: list[pd.DataFrame], s: Settings, items: pd.DataFrame | None = None,
        claims: pd.DataFrame | None = None, cancels: pd.DataFrame | None = None):
    plat = pd.concat([p for p in platforms if not p.empty], ignore_index=True) if any(not p.empty for p in platforms) else pd.DataFrame()
    if plat.empty:
        raise ValueError("No platform orders loaded. Upload at least one DoorDash, Uber Eats or Grubhub CSV.")
    matched = match_orders(toast, plat, s)
    issues = find_issues(toast, matched, s, items, claims, cancels)
    return matched, issues, summarize(toast, matched, issues, s)


def promo_roi(campaigns: pd.DataFrame, commission_rate: float, food_cost_pct: float) -> pd.DataFrame:
    """Return per campaign, including how many of its orders must be truly new for it to pay off.

    DoorDash credits a campaign with every order it touched, including
    customers who would have ordered anyway. So instead of trusting ROAS we
    ask: what share of these orders must be incremental for the profit on
    them to cover the spend? Lower is safer.
    Contribution per $1 of sales = 1 - commission - food & packaging cost.
    """
    if campaigns.empty:
        return campaigns
    c = campaigns.copy()
    c["cost"] = c["discounts_you_fund"] + c["marketing_fees"]
    c["roas"] = c["sales"] / c["cost"].where(c["cost"] > 0)
    margin = max(0.01, 1 - commission_rate - food_cost_pct)
    c["contribution"] = c["sales"] * margin
    c["breakeven_incremental_pct"] = c["cost"] / c["contribution"].where(c["contribution"] > 0)
    c["cost_per_new_customer"] = c["cost"] / c["new_customers"].where(c["new_customers"] > 0)
    c["existing_share"] = c["existing_customers"] / (c["existing_customers"] + c["new_customers"]).where(
        (c["existing_customers"] + c["new_customers"]) > 0)
    return c.sort_values("breakeven_incremental_pct", ascending=False).reset_index(drop=True)


STORE_CAUSED_CANCEL = re.compile(r"wrong order handed|staff requested|out of stock|extreme dasher wait|avoidable store", re.I)
NOT_CONFIRMED = re.compile(r"did not confirm", re.I)


def company_scorecard(claims: pd.DataFrame, waits: pd.DataFrame, cancels: pd.DataFrame) -> pd.DataFrame:
    """One row per store from a multi-store DoorDash Operations Quality export.

    Without the financial report we don't know each store's order count, so
    these are raw counts. Rank within similar-volume stores, or add the
    financial export for rates.
    """
    stores = sorted(set(claims.get("store", pd.Series(dtype=str))) | set(waits.get("store", pd.Series(dtype=str)))
                    | set(cancels.get("store", pd.Series(dtype=str))))
    sc = pd.DataFrame({"store": [s for s in stores if s]}).set_index("store")
    if not claims.empty:
        g = claims.groupby("store")
        sc["error_orders"] = g.size()
        sc["error_charges"] = g["error_charge"].sum()
        sc["missing_item_orders"] = g["error_category"].apply(lambda s: s.str.contains("Missing Item").sum())
        sc["ingredient_side_orders"] = g["error_category"].apply(lambda s: s.str.contains("Ingredient|Side", regex=True).sum())
        sc["top_error_item"] = claims.assign(item=claims["claimed_items"].str.split("; ").str[0].str.replace(r"^\d+ x ", "", regex=True)
                                             .str.replace(r"\s*\(.*$", "", regex=True).str.strip()) \
            .groupby("store")["item"].agg(lambda s: s.value_counts().index[0] if len(s) else "")
    if not waits.empty:
        g = waits.groupby("store")["wait_min"]
        sc["dasher_wait_orders"] = g.size()
        sc["avg_wait_min"] = g.mean()
        sc["waits_over_5min"] = g.apply(lambda s: (s > 5).sum())
        sc["dasher_wait_hours"] = g.sum() / 60
    if not cancels.empty:
        why = cancels["category"] + ": " + cancels["reason"]
        unpaid = cancels[~cancels["paid"]].assign(why=why)
        g = unpaid.groupby("store")
        sc["unpaid_cancels"] = g.size()
        sc["unpaid_cancel_sales"] = g["subtotal"].sum()
        sc["store_caused_cancels"] = g["why"].apply(lambda s: s.str.contains(STORE_CAUSED_CANCEL).sum())
        sc["not_confirmed_cancels"] = g["why"].apply(lambda s: s.str.contains(NOT_CONFIRMED).sum())
    sc = sc.fillna({c: 0 for c in sc.columns if c != "top_error_item"}).reset_index()
    for c in ("error_orders", "missing_item_orders", "ingredient_side_orders", "dasher_wait_orders", "waits_over_5min",
              "unpaid_cancels", "store_caused_cancels", "not_confirmed_cancels"):
        if c in sc:
            sc[c] = sc[c].astype(int)
    return sc.sort_values("error_charges" if "error_charges" in sc else "store", ascending=False).reset_index(drop=True)
