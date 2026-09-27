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
        uploads["toast"] = st.file_uploader("Toast: OrderDetails.csv (optional but recommended)", type="csv", accept_multiple_files=True)
        uploads["toast_items"] = st.file_uploader("Toast: ItemSelectionDetails.csv (proof for 'missing item' charges)", type="csv", accept_multiple_files=True)
        for p, label in LABELS.items():
            uploads[p] = st.file_uploader(f"{label} exports (CSV or zip)" + (": financial + Operations Quality" if p == "doordash" else ""),
                                          type=["csv", "zip"], accept_multiple_files=True)

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
    frames = {k: loaders.read_uploads(v) for k, v in uploads.items()}

tab_results, tab_worklist, tab_matches, tab_howto, tab_columns = st.tabs(
    ["📊 Results", "🧾 Dispute worklist", "🔗 Order matching", "📥 What CSVs to pull", "🧩 Column check"])

with tab_howto:
    st.markdown("""
### Pull these for **one store, the last 30–60 days** (same date range in every file)

| # | File | Where to get it | Must include |
|---|---|---|---|
| 1 | **Toast – Order Details** | Toast Web → Reports → Sales → *Orders* → export CSV (`OrderDetails.csv`) | **Minimum:** Order #, Opened, Amount (matching works on time + amount; proven at 99.8% on Manahawkin). **Better:** Order Id, Tab Names, Dining Options, Order Source, Voided. These tag 3P orders and unlock the "Toast order never paid" check |
| 2 | **DoorDash – Financial report (zip)** | Merchant Portal → Reports → Financial report → pick date range → download. **Upload the zip as-is** | The *Detailed transactions* file inside has everything: order ID, **POS order ID**, Order received time, status, subtotal, commission, store-funded discounts, **error charges, adjustments (dispute credits)**, payout |
| 3 | **Uber Eats – Payment Details** | Uber Eats Manager → Payments → *Reports* → **Payment details** → CSV | Order ID, Order Accept Time, Order Status, Sales (excl. tax), Tax on Sales, **Marketplace Fee**, **Refunds (incl tax)** / order error adjustments, Marketing adjustment, Total payout, Payout Date |
| 4 | **Grubhub – Transactions** | restaurant.grubhub.com → Financials → *Transactions* → export | order_number, transaction_date, transaction_type (incl. *Prepaid Order Adjustment*), subtotal, tax, commission, delivery_commission, processing_fee, merchant_net_total, deposit date, adjustment reason |

**Tips**
1) Use **order-level / transaction-level** exports, not weekly summaries. Summaries can't be matched to orders.
2) Leave column headers as exported. The app finds columns by name and shows anything missing on the **Column check** tab.
3) Pick a store with heavy 3P volume and a single Toast location, and skip any week where the store changed menus or tablets.
4) Nice to have: the store's **bank deposit CSV** for the same period (next version will match payouts to deposits), and Toast's **item-level** export (ItemSelectionDetails), which is the best proof against "missing item" charges.
5) These files contain customer first names. Keep them in the pilot folder and don't email them around.
""")

if not any(frames.get(p) for p in LABELS):
    with tab_results:
        st.warning("Upload at least one platform export (the DoorDash zip works as-is). Add Toast OrderDetails to unlock POS matching. See **What CSVs to pull**.")
    st.stop()
if not frames.get("toast"):
    with tab_results:
        st.info("No Toast file loaded, so POS matching (R2, R3, R4, R7) is skipped. Error charges, credits and commission checks still run.")

toast, toast_map = loaders.load_toast(frames["toast"])
plats, plat_maps = [], {}
for p in LABELS:
    df, mp = loaders.load_platform(frames.get(p, []), p)
    plats.append(df)
    plat_maps[p] = mp

try:
    items = loaders.load_toast_items(frames.get("toast_items", []))
    claims, waits = loaders.load_dd_ops(frames.get("doordash", []))
    matched, issues, s = engine.run(toast, plats, settings, items, claims)
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

    if not waits.empty:
        st.markdown("#### Dashers waiting on food (DoorDash Operations Quality)")
        c = st.columns(3)
        c[0].metric("Orders where the Dasher waited", f"{len(waits):,}")
        c[1].metric("Avg avoidable wait", f"{waits['wait_min'].mean():.1f} min")
        c[2].metric("Waits over 5 min", f"{(waits['wait_min'] > 5).sum():,}")
        st.caption("Food that isn't ready when the Dasher arrives hurts ranking and raises cold-food and missing-item claims. Coach the store on quote times.")

    st.markdown("#### Commission rate mix by week")
    st.caption("Commission ÷ (subtotal − store-funded promo). When one rate column disappears and another appears, "
               "the plan changed. Confirm the franchisee agreed to it.")
    mix = engine.rate_mix(matched)
    if not mix.empty:
        mix = mix.assign(platform=mix["platform"].map(LABELS))
        st.dataframe(mix, hide_index=True, width="stretch")

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
                                "order_link": st.column_config.LinkColumn("portal link", display_text="open"),
                                "evidence": st.column_config.TextColumn(width="large")})
    st.download_button("⬇️ Download worklist CSV", wl.to_csv(index=False), "dispute_worklist.csv", "text/csv")

    st.markdown("### ✍️ Ready to file")
    st.caption("Open disputes with the strongest proof first. Use the copy icon on each box, paste into the platform, then tick it off. "
               "Filing only proven cases protects the store's self-service dispute access.")
    ready = issues[(issues["status"] == "open") & (issues["dispute_text"] != "") & (issues["amount"] > 0)]
    ready = ready.assign(_c=ready["confidence"].map({"high": 0, "medium": 1, "low": 2}).fillna(3)).sort_values(["_c", "days_left"])
    if ready.empty:
        st.success("Nothing open to file right now.")
    for plat in ready["platform"].unique():
        group = ready[ready["platform"] == plat]
        st.markdown(f"**{LABELS.get(plat, plat)}**: {len(group)} to file, ${group['amount'].sum():,.2f}  \n"
                    f"How to file: {engine.FILING_STEPS.get(plat, '')}")
        for _, row in group.head(50).iterrows():
            label = (f"{'✅' if row['confidence'] == 'high' else '⚠️'} {row['platform_order_id']} · ${row['amount']:.2f} · "
                     f"{row['issue']} · {row['days_left']} days left · {row['file_via']}")
            with st.expander(label):
                if isinstance(row.get("order_link"), str) and row["order_link"].startswith("http"):
                    st.link_button("Open order in DoorDash portal", row["order_link"])
                st.code(row["dispute_text"], language=None, wrap_lines=True)
                st.caption(f"Evidence: {row['evidence']}")
        if len(group) > 50:
            st.caption(f"+{len(group) - 50} more in the CSV download.")

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
