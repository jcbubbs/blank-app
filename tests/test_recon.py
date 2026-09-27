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
