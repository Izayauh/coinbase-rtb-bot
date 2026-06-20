from research_pipeline.governance import evaluate_policy_variants

SEC = 1_000_000


def test_policy_tournament_produces_dsr_and_pbo(store):
    for i in range(32):
        t = i * 300 * SEC
        for name, value in {
            "quoted_spread_bps": 0.02,
            "top_of_book_imbalance": 0.3 if i % 2 == 0 else -0.1,
            "signed_trade_flow": 2.0 if i % 3 else -1.0,
            "trade_intensity": 2.0,
        }.items():
            store.insert_feature(name, "v1", "BTC-USD", t, value, 0, "ok", f"{name}:{i}")
        for sensitivity, net in ((1.0, 0.01 if i % 2 == 0 else -0.002),
                                 (2.0, 0.006 if i % 2 == 0 else -0.006)):
            store.insert_label({
                "product_id": "BTC-USD", "decision_time_us": t, "horizon": "5m",
                "entry_side": "BUY", "entry_price": 100.0, "exit_side": "SELL",
                "exit_price": 101.0, "gross_return": 0.01,
                "fee_component": -0.001, "slippage_component": -0.001,
                "adverse_selection_component": -0.001,
                "spread_component": -0.001, "net_return": net,
                "mfe": 0.02, "mae": -0.01, "valid": 1,
                "invalid_reason": None, "quote_source": "ticker",
                "sensitivity": sensitivity, "cost_model_version": "cost_model_v1",
                "replay_version": "replay_v1",
            })
    result = evaluate_policy_variants(store, cscv_slices=4)
    assert result["status"] == "DIAGNOSTIC_ONLY"
    assert result["winner"] in result["policies"]
    assert 0 <= result["dsr"]["probability"] <= 1
    assert 0 <= result["pbo"]["pbo"] <= 1
    assert result["promotion"] == "BLOCKED"
