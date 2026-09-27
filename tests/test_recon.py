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
