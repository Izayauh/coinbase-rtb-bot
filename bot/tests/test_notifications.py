import urllib.parse

from bot.notifications import maybe_send_event_notification


def test_notifications_skip_during_pytest_by_default(monkeypatch):
    calls = []
    monkeypatch.setenv("PUSHOVER_TOKEN", "token")
    monkeypatch.setenv("PUSHOVER_USER", "user")
    monkeypatch.setenv("CB_PUSHOVER_NOTIFY_PAPER", "1")
    monkeypatch.delenv("CB_PUSHOVER_ENABLE_IN_TESTS", raising=False)
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: calls.append((a, k)))

    assert maybe_send_event_notification("ORDER_FILLED", {"symbol": "BTC-USD"}) is False
    assert calls == []


def test_order_filled_notification_posts_to_pushover(monkeypatch):
    sent = {}

    class _Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b"{}"

    def fake_urlopen(req, timeout=0):
        sent["url"] = req.full_url
        sent["timeout"] = timeout
        sent["payload"] = urllib.parse.parse_qs(req.data.decode())
        return _Resp()

    monkeypatch.setenv("PUSHOVER_TOKEN", "token")
    monkeypatch.setenv("PUSHOVER_USER", "user")
    monkeypatch.setenv("CB_PUSHOVER_NOTIFY_PAPER", "1")
    monkeypatch.setenv("CB_PUSHOVER_ENABLE_IN_TESTS", "1")
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    ok = maybe_send_event_notification(
        "ORDER_FILLED",
        {
            "symbol": "BTC-USD",
            "order_id": "ord_1",
            "fill_size": 0.001,
            "fill_price": 65000,
        },
    )

    assert ok is True
    assert sent["url"] == "https://api.pushover.net/1/messages.json"
    assert sent["timeout"] == 10
    assert sent["payload"]["title"] == ["Coinbase BTC-USD order filled"]
    assert "Fill 0.001 at $65,000.00" in sent["payload"]["message"][0]
    assert sent["payload"]["token"] == ["token"]
    assert sent["payload"]["user"] == ["user"]
