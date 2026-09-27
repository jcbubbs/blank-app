from recon import engine, loaders, sample


def _run():
    d = sample.generate(seed=7)
    toast, _ = loaders.load_toast([d["toast"]])
    plats = [loaders.load_platform([d[p]], p)[0] for p in loaders.PLATFORMS]
    return engine.run(toast, plats, engine.Settings())


def test_matches_most_orders():
    matched, _, s = _run()
    assert s["match_rate"] > 0.97


def test_every_error_charge_is_flagged():
    matched, issues, _ = _run()
    assert (matched["error_charge"] < 0).sum() == (issues["rule"] == "R1").sum()
    assert abs(issues.loc[issues["rule"] == "R1", "amount"].sum() + matched["error_charge"].sum()) < 0.01


def test_money_parsing():
    import pandas as pd
    out = loaders.to_money(pd.Series(["$1,234.50", "(12.00)", "", "-3"]))
    assert out.tolist() == [1234.5, -12.0, 0.0, -3.0]


def test_doordash_zip_style_duplicates_and_credits():
    import pandas as pd
    detailed = pd.DataFrame({
        "DoorDash transaction ID": ["t1", "t2", "t3"],
        "DoorDash order ID": ["A1B2C3D4", "A1B2C3D4", "A1B2C3D4"],
        "Order received local time": ["2026-09-01 12:00:00"] * 3,
        "Transaction type": ["Order", "Error Charge", "Adjustment"],
        "Final order status": ["Delivered", "", ""],
        "Subtotal": ["30.00", "0", "0"],
        "Customer discounts from marketing | (funded by you)": ["-5.00", "0", "0"],
        "Commission": ["-5.25", "0", "0"],
        "Error charges": ["0", "-10.00", "0"],
        "Adjustments": ["0", "0", "4.00"],
        "Net total": ["24.75", "-10.00", "4.00"],
    })
    error_file = detailed.iloc[[1, 2]][["DoorDash transaction ID", "DoorDash order ID", "Transaction type",
                                        "Error charges", "Adjustments"]]
    plat, _ = loaders.load_platform([error_file, detailed], "doordash")
    row = plat.iloc[0]
    assert len(plat) == 1
    assert row["error_charge"] == -10.0 and row["adjustment"] == 4.0
    assert row["commission"] == 5.25 and row["promo"] == 5.0
    s = engine.Settings(rates={"doordash": 0.21})
    empty_toast, _ = loaders.load_toast([])
    _, issues, _ = engine.run(empty_toast, [plat], s)
    r1 = issues[issues["rule"] == "R1"].iloc[0]
    assert r1["amount"] == 6.0                      # 10 charged - 4 credited
    assert (issues["rule"] == "R5").sum() == 0      # 5.25 / (30 - 5) = 21% exactly


def test_item_evidence():
    import pandas as pd
    ticket = pd.DataFrame({"item": ["BURRITO", "CHIPS & QUESO 4OZ"], "voided": [False, False],
                           "sent": pd.to_datetime(["2026-09-01 12:01", "2026-09-01 12:01"])})
    ev, conf = engine.item_evidence("1 CHIPS & SIGNATURE QUESO missing", ticket)
    assert conf == "high" and "CHIPS & QUESO 4OZ" in ev
    _, conf = engine.item_evidence("2 TACOS - BUILD YOUR OWN missing", ticket)
    assert conf == "low"
    _, conf = engine.item_evidence("1 BURRITO BOWL - BUILD YOUR OWN missing", ticket.iloc[:0])
    assert conf == ""


def test_ops_quality_enriches_and_downgrades_ingredient_claims():
    import pandas as pd
    fin = pd.DataFrame({
        "DoorDash transaction ID": ["t1", "t2"], "DoorDash order ID": ["4AFF0716", "4AFF0716"],
        "Order received local time": ["2026-09-24 11:05:00"] * 2, "Transaction type": ["Order", "Error Charge"],
        "Final order status": ["Delivered", ""], "Subtotal": ["30.00", "0"], "Commission": ["-6.30", "0"],
        "Error charges": ["0", "-19.70"], "Net total": ["23.70", "-19.70"], "Description": ["", "1 QUESADILLA missing"],
    })
    ops = pd.DataFrame({"DD Order ID": ["4aff0716"], "Error Category": ["Ingredient Error"], "Item Name": ["QUESADILLA"],
                        "Quantity": ["1"], "Customer Comment": ["Requested ranch, got sriracha"], "Dasher Name": ["Uriel L"],
                        "Order Link": ["https://www.doordash.com/merchant/deliveries/x"]})
    plat, _ = loaders.load_platform([fin, ops], "doordash")
    claims, _ = loaders.load_dd_ops([fin, ops])
    toast = pd.DataFrame({"Order #": ["111"], "Opened": ["9/24/26 11:05 AM"], "Amount": ["30.00"]})
    t, _ = loaders.load_toast([toast])
    items = loaders.load_toast_items([pd.DataFrame({"Order #": ["111"], "Sent Date": ["9/24/26 11:05 AM"],
                                                    "Menu Item": ["QUESADILLA"], "Qty": ["1"], "Net Price": ["30"], "Void?": ["false"]})])
    s = engine.Settings(rates={"doordash": 0.21}, as_of=__import__("datetime").date(2026, 9, 27))
    _, issues, _ = engine.run(t, [plat], s, items, claims)
    r1 = issues[issues["rule"] == "R1"].iloc[0]
    assert r1["confidence"] == "medium"
    assert "sriracha" in r1["evidence"] and r1["order_link"].startswith("https://")
