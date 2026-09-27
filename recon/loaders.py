"""Load Toast / DoorDash / Uber Eats / Grubhub CSV exports into one canonical shape.

Export headers vary by report version, so every column is found through an alias
list. Headers are compared after lower-casing and stripping punctuation. Anything
we can't find becomes 0 / empty, and the app reports it under "Column check".
"""
from __future__ import annotations

import re
from io import BytesIO
from typing import Iterable

import pandas as pd

PLATFORMS = ("doordash", "ubereats", "grubhub")

MONEY_FIELDS = (
    "subtotal", "tax", "commission", "processing_fee", "marketing_fee",
    "promo", "error_charge", "adjustment", "net_payout",
)
# Fields where a report splits one concept across several columns (e.g. Grubhub
# "commission" + "delivery_commission"); every matching column is summed.
SUM_FIELDS = {"commission", "processing_fee", "marketing_fee"}

PLATFORM_ALIASES: dict[str, dict[str, list[str]]] = {
    "doordash": {
        "order_id": ["DoorDash order ID", "Order ID", "Delivery UUID"],
        "txn_id": ["DoorDash transaction ID"],
        "pos_order_id": ["POS order ID", "Merchant delivery ID"],
        # "Order received" lines up with Toast's Opened time. "Timestamp" is the
        # transaction time, often 15-20 min later.
        "order_time": ["Order received local time", "Timestamp local time", "Order placed time", "Timestamp local date", "Timestamp UTC time", "Created at"],
        "txn_type": ["Transaction type"],
        "status": ["Final order status", "Order status"],
        "subtotal": ["Subtotal"],
        "tax": ["Subtotal tax passed to merchant", "Tax (subtotal)", "Tax"],
        "commission": ["Commission"],
        "processing_fee": ["Payment processing fee"],
        "marketing_fee": ["Marketing fees | (including any applicable taxes)", "Marketing fees"],
        "promo": ["Customer discounts from marketing | (funded by you)", "Customer discounts funded by you", "Customer discounts"],
        "error_charge": ["Error charges", "Error charge"],
        "adjustment": ["Adjustments"],
        "net_payout": ["Net total", "Net payout"],
        "payout_date": ["Payout date"],
        "payout_id": ["Payout ID"],
        "description": ["Description"],
    },
    "ubereats": {
        "order_id": ["Order ID", "Workflow ID"],
        "txn_id": [],
        "pos_order_id": ["External Order ID"],
        "order_time": ["Order Accept Time", "Order Date"],
        "txn_type": ["Transaction type"],
        "status": ["Order Status"],
        "subtotal": ["Sales (excl. tax)", "Sales excl tax"],
        "tax": ["Tax on Sales"],
        "commission": ["Marketplace Fee"],
        "processing_fee": ["Payment processing fee"],
        "marketing_fee": ["Marketing Adjustment", "Ad spend"],
        "promo": ["Promotions on items", "Offers on items (incl. tax)"],
        "error_charge": ["Order Error Adjustments", "Refunds (incl tax)", "Refunds (incl. tax)"],
        "adjustment": ["Other payments", "Price adjustments (incl. tax)"],
        "net_payout": ["Total payout", "Total payout "],
        "payout_date": ["Payout Date"],
        "payout_id": ["Payout reference ID", "Payout ID"],
        "description": ["Other payments description"],
    },
    "grubhub": {
        "order_id": ["order_number", "Order Number", "Order ID"],
        "txn_id": ["transaction_id"],
        "pos_order_id": [],
        "order_time": ["transaction_date", "Order Date", "time_placed"],
        "txn_type": ["transaction_type", "Transaction Type"],
        "status": ["order_status", "Order Status"],
        "subtotal": ["subtotal", "Subtotal"],
        "tax": ["tax", "Sales Tax"],
        "commission": ["commission", "delivery_commission"],
        "processing_fee": ["processing_fee"],
        "marketing_fee": ["targeted_promotion", "marketing_fee"],
        "promo": ["merchant_funded_promotion"],
        "error_charge": ["merchant_funded_refund", "Order Adjustment"],
        "adjustment": ["adjustment", "misc_adjustment"],
        "net_payout": ["merchant_net_total", "Net Total"],
        "payout_date": ["deposit_date", "Deposit Date"],
        "payout_id": ["deposit_id", "Deposit ID"],
        "description": ["description", "adjustment_reason"],
    },
}

TOAST_ALIASES: dict[str, list[str]] = {
    "toast_order_id": ["Order Id", "Order GUID", "Order ID"],
    "order_number": ["Order #", "Order Number"],
    "opened": ["Opened", "Order Date", "Opened Date"],
    "subtotal": ["Amount", "Subtotal", "Net Amount"],
    "tax": ["Tax"],
    "total": ["Total"],
    "voided": ["Voided"],
    "source": ["Order Source", "Source"],
    "dining_option": ["Dining Options", "Dining Option"],
    "revenue_center": ["Revenue Center"],
    "tab_name": ["Tab Names", "Tab Name", "Check Names"],
    "service": ["Service"],
    "amount_partial": ["amount_partial"],
}

PLATFORM_PATTERNS = {
    "doordash": re.compile(r"door\s*dash|caviar", re.I),
    "ubereats": re.compile(r"uber", re.I),
    "grubhub": re.compile(r"grub\s*hub|seamless", re.I),
}
ADJUSTMENT_TXN = re.compile(r"error|adjust|refund", re.I)


def _norm(s: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def read_csv(src) -> pd.DataFrame:
    """Read a path or an uploaded file; tolerate a BOM and odd encodings."""
    if hasattr(src, "getvalue"):
        src = BytesIO(src.getvalue())
    try:
        return pd.read_csv(src, dtype=str, encoding="utf-8-sig")
    except UnicodeDecodeError:
        if hasattr(src, "seek"):
            src.seek(0)
        return pd.read_csv(src, dtype=str, encoding="latin-1")


def read_uploads(files) -> list[pd.DataFrame]:
    """Read CSVs and zips of CSVs (e.g. DoorDash's financial export zip)."""
    import zipfile
    out = []
    for f in files or []:
        name = getattr(f, "name", str(f)).lower()
        if name.endswith(".zip"):
            data = BytesIO(f.getvalue()) if hasattr(f, "getvalue") else f
            with zipfile.ZipFile(data) as z:
                for member in sorted(z.namelist()):
                    if member.lower().endswith(".csv") and not member.startswith("__MACOSX"):
                        out.append(read_csv(BytesIO(z.read(member))))
        else:
            out.append(read_csv(f))
    return out


def to_money(series: pd.Series) -> pd.Series:
    s = series.fillna("").astype(str).str.strip()
    neg = s.str.startswith("(") & s.str.endswith(")")
    s = s.str.replace(r"[$,()\s]", "", regex=True).replace("", "0")
    out = pd.to_numeric(s, errors="coerce").fillna(0.0)
    return out.where(~neg, -out.abs())


def _find(df: pd.DataFrame, aliases: Iterable[str]) -> list[str]:
    by_norm = {_norm(c): c for c in df.columns}
    return [by_norm[_norm(a)] for a in aliases if _norm(a) in by_norm]


def map_columns(df: pd.DataFrame, aliases: dict[str, list[str]]) -> dict[str, list[str]]:
    """Canonical field -> source columns found (empty list = missing)."""
    return {field: _find(df, names) for field, names in aliases.items()}


def load_platform(frames: list[pd.DataFrame], platform: str) -> tuple[pd.DataFrame, dict]:
    """One row per platform order, money fields signed from the store's view.

    Fees come back positive (a cost to us), error charges come back negative
    (money taken back), and net_payout is what landed in the payout.
    """
    aliases = PLATFORM_ALIASES[platform]
    rows, mapping = [], {}
    for df in frames:
        this_map = map_columns(df, aliases)
        if sum(map(bool, this_map.values())) > sum(map(bool, mapping.values())):
            mapping = this_map  # show the richest file on the Column check tab
        out = pd.DataFrame(index=df.index)
        for field, cols in this_map.items():
            if field in MONEY_FIELDS:
                used = cols if field in SUM_FIELDS else cols[:1]
                out[field] = sum((to_money(df[c]) for c in used), pd.Series(0.0, index=df.index))
            else:
                out[field] = df[cols[0]] if cols else ""
        rows.append(out)
    if not rows:
        return pd.DataFrame(columns=["platform", "order_id", *aliases]), {}
    raw = pd.concat(rows, ignore_index=True)
    raw = raw[raw["order_id"].fillna("").astype(str).str.strip() != ""]
    # DoorDash's zip repeats the same transactions in several files (detailed,
    # simplified, error charges). Keep each transaction ID once.
    # When a transaction repeats, keep the copy with the most filled-in fields,
    # which is the detailed file's.
    filled = (raw.fillna("").astype(str).apply(lambda c: c.str.strip()).ne("") & raw.ne(0)).sum(axis=1)
    raw = raw.assign(_filled=filled).sort_values("_filled", ascending=False, kind="stable")
    has_txn = raw["txn_id"].fillna("").astype(str).str.strip() != ""
    raw = pd.concat([raw[has_txn].drop_duplicates("txn_id"), raw[~has_txn]], ignore_index=True).drop(columns="_filled")

    # Some exports put error charges and adjustments on their own transaction
    # rows, with the amount only in the net column. Move a negative amount into
    # error_charge, and a positive one (e.g. a dispute credit) into adjustment.
    is_adj_row = raw["txn_type"].fillna("").astype(str).str.contains(ADJUSTMENT_TXN)
    empty = is_adj_row & (raw["error_charge"] == 0) & (raw["adjustment"] == 0)
    raw.loc[empty & (raw["net_payout"] < 0), "error_charge"] = raw["net_payout"]
    raw.loc[empty & (raw["net_payout"] > 0), "adjustment"] = raw["net_payout"]
    raw.loc[is_adj_row, "subtotal"] = 0.0

    # Exports show fees as negative numbers. Flip the sign per column rather than
    # taking abs(), so reversal rows from order edits still cancel out.
    for f in ("commission", "processing_fee", "marketing_fee", "promo"):
        if raw[f].sum() < 0:
            raw[f] = -raw[f]
    raw["error_charge"] = raw["error_charge"].where(raw["error_charge"] <= 0, -raw["error_charge"])
    raw["order_time"] = pd.to_datetime(raw["order_time"], errors="coerce", format="mixed")

    agg = {f: "sum" for f in MONEY_FIELDS}
    agg.update({
        "order_time": "min",
        "status": lambda s: next((v for v in s if isinstance(v, str) and v.strip()), ""),
        "txn_type": lambda s: " | ".join(sorted({str(v) for v in s if pd.notna(v) and str(v).strip()})),
        "payout_date": "first", "payout_id": "first", "txn_id": "first",
        "pos_order_id": lambda s: next((v for v in s if isinstance(v, str) and v.strip()), ""),
        "description": lambda s: " | ".join(sorted({str(v) for v in s if pd.notna(v) and str(v).strip()})),
    })
    orders = raw.groupby("order_id", as_index=False).agg(agg)
    orders.insert(0, "platform", platform)
    return orders, mapping


def load_toast(frames: list[pd.DataFrame]) -> tuple[pd.DataFrame, dict]:
    mapping: dict = {}
    parts = []
    for df in frames:
        mapping = map_columns(df, TOAST_ALIASES)
        out = pd.DataFrame(index=df.index)
        for field, cols in mapping.items():
            if field in ("subtotal", "tax", "total"):
                out[field] = to_money(df[cols[0]]) if cols else 0.0
            else:
                out[field] = df[cols[0]].fillna("") if cols else ""
        parts.append(out)
    if not parts:
        return pd.DataFrame(columns=["platform", *TOAST_ALIASES]), {}
    t = pd.concat(parts, ignore_index=True)
    # The simple Orders export has no Order Id column, so fall back to Order #.
    no_id = t["toast_order_id"].astype(str).str.strip() == ""
    t.loc[no_id, "toast_order_id"] = t.loc[no_id, "order_number"].astype(str)
    t = t[t["toast_order_id"].astype(str).str.strip() != ""].drop_duplicates("toast_order_id")
    t["opened"] = pd.to_datetime(t["opened"], errors="coerce", format="mixed")
    t["voided"] = t["voided"].astype(str).str.strip().str.lower().isin({"true", "yes", "1", "y"})
    blob = t[["source", "dining_option", "revenue_center", "tab_name", "service"]].astype(str).agg(" ".join, axis=1)
    t["amount_partial"] = t["amount_partial"].astype(str).str.lower() == "true"
    t["platform"] = ""
    for p, pat in PLATFORM_PATTERNS.items():
        t.loc[(t["platform"] == "") & blob.str.contains(pat), "platform"] = p
    # Came from a marketplace, but we can't tell which one.
    t.loc[(t["platform"] == "") & blob.str.contains(r"rails|3p", case=False), "platform"] = "3p"
    t["search_blob"] = (blob + " " + t["order_number"].astype(str) + " " + t["toast_order_id"].astype(str)).map(lambda s: re.sub(r"[^A-Z0-9 ]", "", s.upper()))
    return t.reset_index(drop=True), mapping


ITEM_ALIASES: dict[str, list[str]] = {
    "order_number": ["Order #", "Order Number"],
    "toast_order_id": ["Order Id", "Order GUID"],
    "sent": ["Sent Date", "Order Date"],
    "item": ["Menu Item", "Item"],
    "qty": ["Qty", "Quantity"],
    "price": ["Net Price", "Gross Price"],
    "voided": ["Void?", "Voided"],
}


def load_toast_items(frames: list[pd.DataFrame]) -> pd.DataFrame:
    """Toast ItemSelectionDetails: one row per item rung on an order."""
    parts = []
    for df in frames:
        mapping = map_columns(df, ITEM_ALIASES)
        out = pd.DataFrame(index=df.index)
        for field, cols in mapping.items():
            out[field] = df[cols[0]].fillna("") if cols else ""
        parts.append(out)
    if not parts:
        return pd.DataFrame(columns=list(ITEM_ALIASES))
    it = pd.concat(parts, ignore_index=True).drop_duplicates()
    it["sent"] = pd.to_datetime(it["sent"], errors="coerce", format="mixed")
    it["price"] = to_money(it["price"])
    it["voided"] = it["voided"].astype(str).str.strip().str.lower().isin({"true", "yes", "1", "y"})
    return it


def _norm_cols(df: pd.DataFrame) -> set[str]:
    return {_norm(c) for c in df.columns}


def load_dd_ops(frames: list[pd.DataFrame]) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """DoorDash Operations Quality export (view by order).

    Returns (claims, waits, cancels):
    - claims: one row per order with the customer's comment, error category,
      items, Dasher and portal link from the missing/incorrect file.
    - waits: orders where the Dasher waited on food (avoidable wait file).
    - cancels: cancelled orders with DoorDash's reason and whether we were paid.
      Unpaid cancels often never appear in the financial report at all.
    Frames that aren't one of these files are ignored, so the whole upload
    list can be passed in.
    """
    claims, waits, cancels = [], [], []
    for df in frames:
        cols = _norm_cols(df)
        if "errorcategory" in cols and "ddorderid" in cols:
            claims.append(df)
        elif "avoidablewaittime" in cols and "ddorderid" in cols:
            waits.append(df)
        elif "cancellationcategoryshort" in cols and "ddorderid" in cols:
            cancels.append(df)

    def pick(df, *names):
        found = _find(df, names)
        return df[found[0]].fillna("").astype(str).str.strip() if found else pd.Series("", index=df.index)

    claim_df = pd.DataFrame(columns=["order_id", "store", "error_charge", "error_category", "claimed_items", "customer_comment", "dasher", "order_link"])
    if claims:
        raw = pd.concat(claims, ignore_index=True).drop_duplicates()
        c = pd.DataFrame({
            "order_id": pick(raw, "DD Order ID").str.upper(),
            "store": pick(raw, "Store Name"),
            "error_charge": to_money(pick(raw, "Error Charge")),
            "error_category": pick(raw, "Error Category"),
            "item": pick(raw, "Quantity") + " x " + pick(raw, "Item Name"),
            "modifier": pick(raw, "Modifier Detail"),
            "customer_comment": pick(raw, "Customer Comment"),
            "dasher": pick(raw, "Dasher Name"),
            "order_link": pick(raw, "Order Link"),
        })
        c["item"] = c["item"].str.strip() + c["modifier"].map(lambda m: f" ({m})" if m else "")
        join = lambda s: "; ".join(dict.fromkeys(v for v in s if v))
        claim_df = c.groupby("order_id", as_index=False).agg(
            store=("store", "first"), error_charge=("error_charge", "sum"),
            error_category=("error_category", join), claimed_items=("item", join),
            customer_comment=("customer_comment", join), dasher=("dasher", join), order_link=("order_link", "first"))

    wait_df = pd.DataFrame(columns=["order_id", "store", "wait_min", "food_ready", "dasher_arrived", "delivered_date"])
    if waits:
        raw = pd.concat(waits, ignore_index=True).drop_duplicates()
        wait_df = pd.DataFrame({
            "order_id": pick(raw, "DD Order ID").str.upper(),
            "store": pick(raw, "Store Name"),
            "wait_min": pd.to_numeric(pick(raw, "Avoidable Wait Time"), errors="coerce"),
            "food_ready": pd.to_datetime(pick(raw, "Confirmed Food Ready time"), errors="coerce", format="mixed"),
            "dasher_arrived": pd.to_datetime(pick(raw, "Dasher Arrival Time"), errors="coerce", format="mixed"),
            "delivered_date": pick(raw, "Order Delivered Date"),
        })
    cancel_df = pd.DataFrame(columns=["order_id", "store", "placed", "category", "reason", "paid", "subtotal", "net_payout"])
    if cancels:
        raw = pd.concat(cancels, ignore_index=True).drop_duplicates()
        cancel_df = pd.DataFrame({
            "order_id": pick(raw, "DD Order ID").str.upper(),
            "store": pick(raw, "Store Name"),
            "placed": pd.to_datetime(pick(raw, "Order Placed Date") + " " + pick(raw, "Order Placed Time"), errors="coerce", format="mixed"),
            "category": pick(raw, "Cancellation Category - Short"),
            "reason": pick(raw, "Non-payment reason"),
            "paid": pick(raw, "Paid").str.lower().isin({"true", "yes", "1"}),
            "subtotal": to_money(pick(raw, "Order Subtotal")),
            "net_payout": to_money(pick(raw, "Net Payout")),
        })
    return claim_df, wait_df, cancel_df


def toast_orders_from_items(items: pd.DataFrame) -> pd.DataFrame:
    """Rebuild an order list from ItemSelectionDetails when OrderDetails is missing.

    Item rows don't carry paid modifier prices, so the rebuilt amount can come
    in under the real subtotal. amount_partial marks that so the matcher
    allows it. Orders with an Olo "Rails Markup" line came in from a
    marketplace, so they're tagged "3p".
    """
    if items.empty:
        return pd.DataFrame(columns=["Order #", "Opened", "Amount", "Order Source"])
    live = items[~items["voided"]]
    o = live.groupby("order_number").agg(
        opened=("sent", "min"), amount=("price", "sum"),
        rails=("item", lambda s: s.astype(str).str.contains("rails markup", case=False).any())).reset_index()
    return pd.DataFrame({
        "Order #": o["order_number"], "Opened": o["opened"].dt.strftime("%m/%d/%y %I:%M %p"),
        "Amount": o["amount"].round(2), "Order Source": o["rails"].map({True: "Olo Rails (3P)", False: ""}),
        "amount_partial": True,
    })


def load_dd_marketing(frames: list[pd.DataFrame]) -> pd.DataFrame:
    """DoorDash Marketing export (Promotion + Sponsored Listing), one row per campaign."""
    rows = []
    for df in frames:
        cols = _norm_cols(df)
        if "campaignname" not in cols or "sales" not in cols:
            continue
        kind = "Sponsored listing (ads)" if "impressions" in cols else None

        def num(*names):
            found = _find(df, names)
            return to_money(df[found[0]]) if found else pd.Series(0.0, index=df.index)

        def txt(*names):
            found = _find(df, names)
            return df[found[0]].fillna("").astype(str) if found else pd.Series("", index=df.index)

        rows.append(pd.DataFrame({
            "campaign": txt("Campaign name").str.strip(),
            "type": kind or txt("Type of promotion"),
            "orders": num("Orders"),
            "sales": num("Sales"),
            "discounts_you_fund": num("Customer discounts from marketing | (Funded by you)").abs(),
            "marketing_fees": num("Marketing fees | (including any applicable taxes)").abs(),
            "new_customers": num("New customers acquired"),
            "existing_customers": num("Existing customers acquired"),
        }))
    if not rows:
        return pd.DataFrame(columns=["campaign", "type", "orders", "sales", "discounts_you_fund", "marketing_fees",
                                     "new_customers", "existing_customers"])
    return pd.concat(rows, ignore_index=True).groupby(["campaign", "type"], as_index=False).sum()
