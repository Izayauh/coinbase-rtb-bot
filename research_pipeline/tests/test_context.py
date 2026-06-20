"""Context track tests: provenance, vintage, availability filtering, access gaps."""
import pytest

from research_pipeline.context import (
    BLSCPIAdapter, CFTCBitcoinCOTAdapter, EdgarCoinbaseAdapter, FederalReserveRSSAdapter,
    CoinbaseIntxDerivativesAdapter, CoinbaseStatusRSSAdapter, CoinDeskRSSAdapter,
    FixtureFOMCAdapter, NotWiredAdapter,
    AccessGap, ContextRecord, CONTEXT_SOURCES,
)
import research_pipeline.context.base as context_base

JAN_AVAIL = 1769626800000000
MAR_AVAIL = 1773856800000000


def test_fixture_fomc_fetch_full_window():
    recs = FixtureFOMCAdapter().fetch(since_us=0, until_us=2 * 10**18)
    assert len(recs) == 2
    r = recs[0]
    # full provenance is present
    assert r.source_id == "fomc" and r.native_id and r.vintage
    assert r.availability_time_us and r.url.startswith("https://")
    assert r.parser_version and isinstance(r.payload, dict)


def test_availability_filtering_respects_window():
    only_jan = FixtureFOMCAdapter().fetch(since_us=JAN_AVAIL - 1, until_us=JAN_AVAIL + 1)
    assert len(only_jan) == 1 and only_jan[0].native_id == "FOMC-2026-01"


def test_payload_hash_is_stable():
    r = FixtureFOMCAdapter().fetch(0, 2 * 10**18)[0]
    assert r.payload_sha256() == r.payload_sha256()
    assert "payload_sha256" in r.to_row()


def test_not_wired_adapter_raises_access_gap():
    adapter = NotWiredAdapter("cpi_bls", "cpi_bls", "BLS API not wired")
    with pytest.raises(AccessGap):
        adapter.fetch(0, 10**18)


def test_context_sources_enumerated():
    assert set(CONTEXT_SOURCES) == {
        "fomc", "cpi_bls", "edgar", "cftc", "funding_oi",
        "official_exchange", "attributed_news", "onchain",
    }


def test_bls_adapter_uses_retrieval_time_as_conservative_availability(monkeypatch):
    monkeypatch.setattr(context_base.time, "time", lambda: 1_800_000_000)
    monkeypatch.setattr(context_base, "_get_json", lambda *a, **k: {
        "status": "REQUEST_SUCCEEDED",
        "Results": {"series": [{"data": [{
            "year": "2026", "period": "M05", "periodName": "May",
            "value": "320.0", "footnotes": [],
        }]}]},
    })
    avail = 1_800_000_000 * 1_000_000
    records = BLSCPIAdapter().fetch(avail - 1, avail + 1)
    assert len(records) == 1
    assert records[0].payload["availability_basis"] == "retrieval_time_conservative"


def test_edgar_adapter_uses_acceptance_timestamp(monkeypatch):
    monkeypatch.setattr(context_base, "_get_json", lambda *a, **k: {
        "filings": {"recent": {
            "accessionNumber": ["0001679788-26-000001"],
            "filingDate": ["2026-06-01"],
            "reportDate": ["2026-05-31"],
            "acceptanceDateTime": ["2026-06-01T18:30:00.000Z"],
            "act": ["34"], "form": ["8-K"], "fileNumber": ["001"],
            "filmNumber": ["1"], "items": ["2.02"], "size": [123],
            "isXBRL": [1], "isInlineXBRL": [1],
            "primaryDocument": ["coin-20260601.htm"],
            "primaryDocDescription": ["8-K"],
        }}
    })
    records = EdgarCoinbaseAdapter().fetch(0, 2 * 10**18)
    assert len(records) == 1
    assert records[0].native_id == "0001679788-26-000001"
    assert records[0].url.startswith("https://www.sec.gov/Archives/")


def test_fed_rss_adapter_parses_official_feed_shape(monkeypatch):
    xml = b"""<rss><channel><item>
      <title>Federal Reserve issues FOMC statement</title>
      <link>https://www.federalreserve.gov/newsevents/pressreleases/test.htm</link>
      <guid>fed-test</guid>
      <pubDate>Wed, 17 Jun 2026 18:00:00 GMT</pubDate>
      <description>Statement text</description>
    </item></channel></rss>"""
    monkeypatch.setattr(context_base, "_get_bytes", lambda *a, **k: xml)
    records = FederalReserveRSSAdapter().fetch(0, 2 * 10**18)
    assert len(records) == 1
    assert records[0].native_id == "fed-test"


def test_coinbase_status_rss_preserves_first_seen_and_revision(monkeypatch):
    xml = b"""<rss><channel><item>
      <title>Advanced Trade degraded performance</title>
      <link>https://status.coinbase.com/incidents/test</link>
      <guid>incident-test</guid>
      <pubDate>Fri, 19 Jun 2026 18:00:00 GMT</pubDate>
      <description>Investigating</description>
    </item></channel></rss>"""
    monkeypatch.setattr(context_base, "_get_bytes", lambda *a, **k: xml)
    monkeypatch.setattr(context_base.time, "time", lambda: 1_800_000_000)
    records = CoinbaseStatusRSSAdapter().fetch(0, 2 * 10**18)
    assert len(records) == 1
    assert records[0].source_kind == "official_exchange"
    assert records[0].payload["first_seen_time_us"] == 1_800_000_000_000_000
    assert records[0].payload["revision_hash"] == records[0].vintage


def test_coindesk_rss_is_attributed_news(monkeypatch):
    xml = b"""<rss><channel><item>
      <title>Bitcoin market report</title>
      <link>https://www.coindesk.com/markets/test</link>
      <guid>coindesk-test</guid>
      <pubDate>Fri, 19 Jun 2026 19:00:00 GMT</pubDate>
      <description>Attributed report</description>
      <category>Markets</category>
    </item></channel></rss>"""
    monkeypatch.setattr(context_base, "_get_bytes", lambda *a, **k: xml)
    records = CoinDeskRSSAdapter().fetch(0, 2 * 10**18)
    assert len(records) == 1
    assert records[0].source_id == "news_coindesk"
    assert records[0].source_kind == "attributed_news"


def test_cftc_adapter_filters_bitcoin_contracts(monkeypatch):
    import csv
    import io
    import zipfile

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=[
        "Market_and_Exchange_Names",
        "Report_Date_as_YYYY-MM-DD",
        "Open_Interest_All",
    ])
    writer.writeheader()
    writer.writerow({
        "Market_and_Exchange_Names": "MICRO BITCOIN - CHICAGO MERCANTILE EXCHANGE",
        "Report_Date_as_YYYY-MM-DD": "2026-06-16",
        "Open_Interest_All": "12345",
    })
    writer.writerow({
        "Market_and_Exchange_Names": "S&P 500 - CHICAGO MERCANTILE EXCHANGE",
        "Report_Date_as_YYYY-MM-DD": "2026-06-16",
        "Open_Interest_All": "999",
    })
    archive_bytes = io.BytesIO()
    with zipfile.ZipFile(archive_bytes, "w") as archive:
        archive.writestr("FinFutYY.txt", output.getvalue())
    monkeypatch.setattr(context_base, "_get_bytes", lambda *a, **k: archive_bytes.getvalue())
    monkeypatch.setattr(context_base.time, "time", lambda: 1_800_000_000)
    avail = 1_800_000_000 * 1_000_000
    records = CFTCBitcoinCOTAdapter(year=2026).fetch(avail - 1, avail + 1)
    assert len(records) == 1
    assert "BITCOIN" in records[0].payload["Market_and_Exchange_Names"]


def test_coinbase_intx_adapter_emits_open_interest_and_funding(monkeypatch):
    monkeypatch.setattr(context_base.time, "time", lambda: 1_800_000_000)

    def fake_json(url, **_kwargs):
        if url.endswith("/funding"):
            return {"results": [{
                "instrument_id": "btc-perp-id",
                "funding_rate": "-0.000005",
                "mark_price": "63458.2",
                "event_time": "2026-06-20T01:00:00Z",
            }]}
        return {
            "open_interest": "2848.2331",
            "qty_24hr": "100",
            "notional_24hr": "200",
            "trading_state": "trading",
            "funding_interval": "3600000000000",
        }

    monkeypatch.setattr(context_base, "_get_json", fake_json)
    avail = 1_800_000_000 * 1_000_000
    records = CoinbaseIntxDerivativesAdapter().fetch(avail - 1, avail + 1)
    assert len(records) == 2
    assert records[0].payload["open_interest"] == "2848.2331"
    assert records[1].payload["funding_rate"] == "-0.000005"
    assert all(
        record.payload["availability_basis"] == "retrieval_time_conservative"
        for record in records
    )
