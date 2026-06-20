"""Executable labeler + replay tests: friction accounting, validity, determinism."""
from research_pipeline.config import CostModel
from research_pipeline.labeling import build_labels

SEC = 1_000_000  # microseconds per second
STALE = 2 * SEC


def seed_quotes(store, run_id, t0, n=13, dt=30, price0=100.0, step=0.1):
    """n quotes spaced dt seconds apart; mid drifts up by `step` per quote."""
    rid, _ = store.append_raw(run_id, "coinbase_ws", "ticker", {"seed": t0}, t0, t0)
    for k in range(n):
        t = t0 + k * dt * SEC
        mid = price0 + k * step
        store.insert_quote("BTC-USD", best_bid=mid - 0.05, best_bid_qty=1.0,
                           best_ask=mid + 0.05, best_ask_qty=1.0,
                           event_time_us=t, recv_time_us=t, raw_id=rid)


def test_valid_label_friction_decomposition(store, run_id):
    t0 = 10 * SEC
    seed_quotes(store, run_id, t0)  # quotes at t0 .. t0+360s
    cm = CostModel()  # 60 bps taker, 2 bps slip, 2 bps adverse
    labels = build_labels(store, "BTC-USD", [t0], {"5m": 300}, cm, STALE,
                          sensitivities=[1.0], persist=False)
    assert len(labels) == 1
    lb = labels[0]
    assert lb["valid"] == 1 and lb["invalid_reason"] is None
    # entry buys the ask (+slip), exit sells the bid (-slip)
    assert lb["entry_side"] == "BUY" and lb["exit_side"] == "SELL"
    # additive first-order decomposition: net == gross + all cost components
    recombined = (lb["gross_return"] + lb["spread_component"] + lb["slippage_component"]
                  + lb["fee_component"] + lb["adverse_selection_component"])
    assert abs(lb["net_return"] - recombined) < 1e-12
    # all cost components are <= 0; net < gross (friction is a drag)
    for c in ("spread_component", "slippage_component", "fee_component",
              "adverse_selection_component"):
        assert lb[c] <= 0
    assert lb["net_return"] < lb["gross_return"]
    # a 1% move is eaten by ~126 bps round-trip friction -> net negative
    assert lb["net_return"] < 0


def test_higher_sensitivity_costs_more(store, run_id):
    t0 = 10 * SEC
    seed_quotes(store, run_id, t0)
    cm = CostModel()
    labels = build_labels(store, "BTC-USD", [t0], {"5m": 300}, cm, STALE,
                          sensitivities=[1.0, 2.0], persist=False)
    by_sens = {l["sensitivity"]: l for l in labels}
    assert by_sens[2.0]["net_return"] < by_sens[1.0]["net_return"]


def test_no_row_when_horizon_unavailable(store, run_id):
    t0 = 10 * SEC
    seed_quotes(store, run_id, t0)  # data ends at t0+360s
    last_t = t0 + 12 * 30 * SEC
    labels = build_labels(store, "BTC-USD", [last_t], {"5m": 300}, CostModel(), STALE,
                          persist=False)
    assert labels == []  # t+5m is beyond available data -> no row


def test_stale_quote_is_invalid(store, run_id):
    t0 = 10 * SEC
    seed_quotes(store, run_id, t0)
    decision = t0 + 5 * SEC  # 5s after the last quote -> > 2s staleness
    labels = build_labels(store, "BTC-USD", [decision], {"5m": 300}, CostModel(), STALE,
                          persist=False)
    assert len(labels) == 1 and labels[0]["valid"] == 0
    assert labels[0]["invalid_reason"] == "STALE_QUOTE"


def test_crossed_quote_is_invalid(store, run_id):
    t0 = 10 * SEC
    rid, _ = store.append_raw(run_id, "coinbase_ws", "ticker", {"x": 1}, t0, t0)
    # entry quote crossed (bid >= ask)
    store.insert_quote("BTC-USD", 100.10, 1.0, 100.0, 1.0, t0, t0, rid)
    # normal later quotes so the horizon is available
    for k in range(1, 13):
        t = t0 + k * 30 * SEC
        store.insert_quote("BTC-USD", 100.0, 1.0, 100.1, 1.0, t, t, rid)
    labels = build_labels(store, "BTC-USD", [t0], {"5m": 300}, CostModel(), STALE,
                          persist=False)
    assert len(labels) == 1 and labels[0]["invalid_reason"] == "CROSSED"


def test_replay_is_deterministic(store, run_id):
    t0 = 10 * SEC
    seed_quotes(store, run_id, t0)
    args = ("BTC-USD", [t0, t0 + 30 * SEC], {"5m": 300, "15m": 900}, CostModel(), STALE)
    a = build_labels(store, *args, persist=False)
    b = build_labels(store, *args, persist=False)
    assert a == b  # identical inputs -> identical labels (live/replay parity by construction)
