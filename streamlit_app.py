"""3P Recovery pilot: reconcile one store's Toast + delivery-platform CSVs."""
from datetime import date

import pandas as pd
import streamlit as st

from recon import engine, loaders, sample

st.set_page_config(page_title="3P Recovery Pilot", page_icon="🌯", layout="wide")
st.title("🌯 3P Recovery Pilot")
st.caption("Reconcile one store's Toast orders against DoorDash, Uber Eats and Grubhub payouts and find money to claw back.")

LABELS = {"doordash": "DoorDash", "ubereats": "Uber Eats", "grubhub": "Grubhub"}

# ---------- Sidebar: data + settings ----------
with st.sidebar:
    st.header("1. Data")
    mode = st.radio("Source", ["Sample data (demo)", "Upload my CSVs"], index=0)
    uploads = {}
    if mode == "Upload my CSVs":
        uploads["toast"] = st.file_uploader("Toast: OrderDetails.csv", type="csv", accept_multiple_files=True)
        for p, label in LABELS.items():
            uploads[p] = st.file_uploader(f"{label} financial CSV", type="csv", accept_multiple_files=True)

    st.header("2. Your contract terms")
    rates, windows = {}, {}
    for p, label in LABELS.items():
        c1, c2 = st.columns(2)
        rates[p] = c1.number_input(f"{label} commission %", 0.0, 40.0, engine.DEFAULT_RATES[p] * 100, 0.5) / 100
        windows[p] = c2.number_input(f"{label} dispute days", 1, 90, engine.DEFAULT_WINDOWS[p])
    win_rate = st.slider("Assumed dispute win rate", 0.0, 1.0, 0.60, 0.05)
    as_of = st.date_input("Deadlines measured as of", date.today())

settings = engine.Settings(rates=rates, windows=windows, win_rate=win_rate, as_of=as_of)


@st.cache_data
def sample_frames():
    return sample.generate()


if mode == "Sample data (demo)":
    frames = {k: [v] for k, v in sample_frames().items()}
    st.info("Showing **made-up sample data** with planted problems. Switch to *Upload my CSVs* in the sidebar to run a real store.")
else:
    frames = {k: [loaders.read_csv(f) for f in (v or [])] for k, v in uploads.items()}

tab_results, tab_worklist, tab_matches, tab_howto, tab_columns = st.tabs(
    ["📊 Results", "🧾 Dispute worklist", "🔗 Order matching", "📥 What CSVs to pull", "🧩 Column check"])

with tab_howto:
    st.markdown("""
### Pull these for **one store, the last 30–60 days** (same date range in every file)

| # | File | Where to get it | Must include |
|---|---|---|---|
| 1 | **Toast – Order Details** | Toast Web → Reports → Sales → *Orders* (Order Details) → export CSV (`OrderDetails.csv`) | Order Id, Order #, Opened, **Tab Names**, Dining Options, Revenue Center, **Order Source**, Amount, Tax, Total, Voided |
| 2 | **DoorDash – Transactions / Financial detail** | Merchant Portal → Financials → *Transactions* (or Reports → Financial report) → download CSV | DoorDash order ID, Timestamp local time, Transaction type, Final order status, Subtotal, Tax, **Commission**, Marketing fees, **Error charges**, Adjustments, Net total, Payout date / ID, Description |
| 3 | **Uber Eats – Payment Details** | Uber Eats Manager → Payments → *Reports* → **Payment details** → CSV | Order ID, Order Accept Time, Order Status, Sales (excl. tax), Tax on Sales, **Marketplace Fee**, **Refunds (incl tax)** / order error adjustments, Marketing adjustment, Total payout, Payout Date |
| 4 | **Grubhub – Transactions** | restaurant.grubhub.com → Financials → *Transactions* → export | order_number, transaction_date, transaction_type (incl. *Prepaid Order Adjustment*), subtotal, tax, commission, delivery_commission, processing_fee, merchant_net_total, deposit date, adjustment reason |

**Tips**
1) Use **order-level / transaction-level** exports, not weekly summaries. Summaries can't be matched to orders.
2) Leave column headers as exported. The app finds columns by name and shows anything missing on the **Column check** tab.
3) Pick a store with heavy 3P volume and a single Toast location, and skip any week where the store changed menus or tablets.
4) Nice to have: the store's **bank deposit CSV** for the same period (next version will match payouts to deposits).
5) These files contain customer first names. Keep them in the pilot folder and don't email them around.
""")

if not frames.get("toast") or not any(frames.get(p) for p in LABELS):
    with tab_results:
        st.warning("Upload the Toast OrderDetails CSV plus at least one platform CSV to run the reconciliation. See **What CSVs to pull**.")
    st.stop()

toast, toast_map = loaders.load_toast(frames["toast"])
plats, plat_maps = [], {}
for p in LABELS:
    df, mp = loaders.load_platform(frames.get(p, []), p)
    plats.append(df)
    plat_maps[p] = mp

try:
    matched, issues, s = engine.run(toast, plats, settings)
except ValueError as e:
    st.error(str(e))
    st.stop()

money = "${:,.0f}".format

with tab_results:
    st.subheader(f"{s['days']} days · {s['orders']:,} delivered 3P orders · {money(s['sales'])} 3P sales")
    c = st.columns(4)
    c[0].metric("Flagged $ (all issues)", money(s["flagged"]), f"{s['pct_of_sales']:.1%} of 3P sales", delta_color="off")
    c[1].metric("Still disputable now", money(s["open"]), f"expect ~{money(s['expected_recovery'])} at {win_rate:.0%} win", delta_color="off")
    c[2].metric("Lost to expired deadlines", money(s["expired"]), "the cost of not checking weekly", delta_color="inverse")
    c[3].metric("Annualized recovery (this store)", money(s["annualized"]), "flagged × win rate × 365/days", delta_color="off")
    c = st.columns(4)
    c[0].metric("Orders matched to Toast", f"{s['match_rate']:.1%}")
    c[1].metric("Error charges taken", money(s["error_charges"]))
    c[2].metric("Platform fees paid", money(s["fees"]))
    c[3].metric("Fees as % of 3P sales", f"{(s['fees'] / s['sales'] if s['sales'] else 0):.1%}")

    st.markdown("#### What we found")
    by_rule = (issues.assign(open_amt=issues["amount"].where(issues["status"] == "open", 0))
               .groupby(["rule", "issue"], as_index=False)
               .agg(cases=("amount", "size"), total=("amount", "sum"), still_disputable=("open_amt", "sum"))
               .sort_values("total", ascending=False))
    st.dataframe(by_rule, hide_index=True, width="stretch",
                 column_config={"total": st.column_config.NumberColumn(format="$%.2f"),
                                "still_disputable": st.column_config.NumberColumn(format="$%.2f")})
    by_plat = (issues[issues["rule"].isin(engine.RECOVERABLE_RULES)]
               .groupby("platform", as_index=False).agg(cases=("amount", "size"), total=("amount", "sum")))
    st.dataframe(by_plat.assign(platform=by_plat["platform"].map(LABELS)), hide_index=True,
                 column_config={"total": st.column_config.NumberColumn(format="$%.2f")})
    st.caption("R3 (platform order not in Toast) carries $0. It's an ops, inventory and tax flag, not a dispute. "
               "Annualized assumes the same error rate all year and the win rate set in the sidebar.")

with tab_worklist:
    st.markdown("Filed from the top down: **open cases, soonest deadline first.** Download this and work it in each platform's merchant portal.")
    only_open = st.checkbox("Only open (not expired)", value=True)
    only_rec = st.checkbox("Only disputable issues (hide R3 ops flags)", value=True)
    wl = issues
    if only_open:
        wl = wl[wl["status"] == "open"]
    if only_rec:
        wl = wl[wl["rule"].isin(engine.RECOVERABLE_RULES)]
    wl = wl.assign(platform=wl["platform"].map(LABELS))
    st.dataframe(wl, hide_index=True, width="stretch",
                 column_config={"amount": st.column_config.NumberColumn(format="$%.2f"),
                                "evidence": st.column_config.TextColumn(width="large")})
    st.download_button("⬇️ Download worklist CSV", wl.to_csv(index=False), "dispute_worklist.csv", "text/csv")

with tab_matches:
    st.markdown("How each platform order lined up with Toast. **T3 loose** and unmatched rows are worth a manual look.")
    st.dataframe(matched["match_tier"].fillna("Unmatched").value_counts().rename_axis("tier").reset_index(name="orders"),
                 hide_index=True)
    view = matched.merge(toast[["toast_order_id", "order_number", "opened", "subtotal", "tab_name"]],
                         on="toast_order_id", how="left", suffixes=("", "_toast"))
    st.dataframe(view[["platform", "order_id", "order_time", "status", "subtotal", "commission", "error_charge",
                       "net_payout", "match_tier", "order_number", "opened", "subtotal_toast", "tab_name"]],
                 hide_index=True, width="stretch")

with tab_columns:
    st.markdown("Which export columns were picked up. **Missing required fields make results wrong**, so fix those before trusting the numbers.")
    required = {"toast_order_id", "opened", "subtotal", "voided", "order_id", "order_time", "status", "commission", "error_charge", "net_payout"}
    rows = [("Toast", f, ", ".join(c) or "— missing", f in required) for f, c in toast_map.items()]
    for p, mp in plat_maps.items():
        rows += [(LABELS[p], f, ", ".join(c) or "— missing", f in required) for f, c in mp.items()]
    cols = pd.DataFrame(rows, columns=["file", "field", "found as", "required"])
    st.dataframe(cols, hide_index=True, width="stretch")
    missing = cols[(cols["found as"] == "— missing") & cols["required"]]
    if not missing.empty:
        st.error(f"Missing required columns: {', '.join(missing['file'] + ' → ' + missing['field'])}. "
                 "Send me the header row and I'll add the alias.")
