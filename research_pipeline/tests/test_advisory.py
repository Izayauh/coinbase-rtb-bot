import json
from pathlib import Path

from research_pipeline.advisory import (
    CandidateAdvisoryPublisher,
    upload_advisory,
    validate_advisory,
)
from research_pipeline.candidates.derivatives_stress import (
    MIN_FUNDING_HISTORY,
    MIN_MINUTE_HISTORY,
)


MINUTE = 60_000_000


def _row(index):
    mid = 100.0 + index * 0.001 + ((index % 17) - 8) * 0.01
    oi = 1000.0 + index * 0.01 + ((index % 19) - 9) * 0.2
    return {
        "event_time_us": index * MINUTE,
        "mid": mid,
        "open_interest": oi,
        "funding_event_time_us": (index // 60) * 60 * MINUTE,
        "funding_rate": ((index // 60) % 11 - 5) * 0.00001,
        "mark_price": mid * (1 + ((index % 13) - 6) * 0.0001),
        "depth_imbalance_10bps": -0.2,
        "ofi_60s": -1.0,
        "microprice_delta_bps": -0.1,
        "bid_replenishment_ratio": 0.9,
        "ask_replenishment_ratio": 1.1,
    }


def _insert_context(store, event_time, open_interest, funding, mark):
    base = {
        "source_id": "coinbase_intx_btc_perp",
        "source_kind": "funding_oi",
        "vintage": "v1",
        "availability_time_us": event_time,
        "url": "https://api.international.coinbase.com/test",
        "parser_version": "test",
    }
    store.insert_context({
        **base,
        "native_id": f"BTC-PERP:open_interest:{event_time // MINUTE}",
        "event_time_us": event_time,
        "payload": {"open_interest": open_interest},
    })
    store.insert_context({
        **base,
        "native_id": f"BTC-PERP:funding:{event_time}",
        "event_time_us": event_time,
        "payload": {"funding_rate": funding, "mark_price": mark},
    })


def test_online_advisory_emits_hash_valid_combined_signal(store, tmp_path):
    total = max(
        MIN_MINUTE_HISTORY + 20,
        MIN_FUNDING_HISTORY * 60 + 20,
    )
    history = [_row(index) for index in range(total)]
    current = _row(total)
    current["mid"] *= 0.97
    current["open_interest"] *= 0.97
    current["funding_rate"] = -0.001
    current["mark_price"] = current["mid"] * 0.98
    current.update({
        "product_id": "BTC-USD",
        "flags": "ok",
        "depth_imbalance_10bps": 0.3,
        "ofi_60s": 10.0,
        "microprice_delta_bps": 0.2,
        "bid_replenishment_ratio": 1.4,
        "ask_replenishment_ratio": 0.8,
    })
    _insert_context(
        store,
        current["event_time_us"],
        current["open_interest"],
        current["funding_rate"],
        current["mark_price"],
    )
    output = tmp_path / "advisory.json"
    publisher = CandidateAdvisoryPublisher(
        store,
        history_rows=history,
        output_path=output,
    )

    payload = publisher.observe(current)

    assert payload["status"] == "SIGNAL"
    assert payload["variant"] == "combined_strict_v1"
    assert payload["exit_contract"]["exit_contract_id"] == (
        "derivatives_stress_exit_v1"
    )
    assert payload["exit_contract"]["time_stop_seconds"] == 14400
    assert payload["live_authority_granted"] is False
    assert validate_advisory(payload) == (True, "advisory valid")
    assert json.loads(output.read_text()) == payload


class _Blob:
    def __init__(self, name, objects):
        self.name = name
        self.objects = objects
        self.metadata = {}
        self.content_type = None
        self.size = None

    def upload_from_string(self, body, **_kwargs):
        self.objects[self.name] = {
            "body": bytes(body),
            "metadata": dict(self.metadata),
        }
        self.size = len(body)

    def reload(self):
        item = self.objects[self.name]
        self.size = len(item["body"])
        self.metadata = dict(item["metadata"])


class _Bucket:
    def __init__(self, objects):
        self.objects = objects

    def blob(self, name):
        return _Blob(name, self.objects)


class _Client:
    def __init__(self):
        self.objects = {}

    def bucket(self, _name):
        return _Bucket(self.objects)


def test_advisory_upload_verifies_remote_hash(tmp_path):
    path = tmp_path / "advisory.json"
    path.write_text('{"status":"NO_SIGNAL"}\n')
    client = _Client()
    result = upload_advisory(
        path,
        bucket="research",
        object_name="advisory/latest.json",
        project="bitwise-trader",
        client=client,
    )
    assert result["bytes"] == path.stat().st_size
    assert (
        client.objects["advisory/latest.json"]["metadata"]["sha256"]
        == result["sha256"]
    )
