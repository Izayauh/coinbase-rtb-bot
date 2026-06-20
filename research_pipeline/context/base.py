"""
Authoritative context track — adapter contract + provenance.

Context (FOMC/CPI/EDGAR/CFTC/funding/on-chain) enters the test-off as annotations,
suppressors, and regime tags ONLY — never as an order source (contract §16).

Every context observation carries deterministic provenance: source, native id, vintage
(for revisions), availability time (when it was publicly knowable), event time (the subject
time), URL/endpoint, payload + hash, and parser version. An LLM may later annotate stored
evidence, but the raw source and metadata remain primary (never an LLM summary as canonical).

This pass ships:
  * the ContextRecord/ContextAdapter contract;
  * one WORKING fixture-backed adapter (FOMC) so the contract is exercised offline;
  * NotWiredAdapter for sources that are not yet wired — they raise AccessGap rather than
    fabricate data. No low-quality scraped sentiment is substituted to claim completion.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import time
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from abc import ABC, abstractmethod
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional

CONTEXT_SOURCES = (
    "fomc",
    "cpi_bls",
    "edgar",
    "cftc",
    "funding_oi",
    "official_exchange",
    "attributed_news",
    "onchain",
)


class AccessGap(RuntimeError):
    """Raised when a context source cannot be accessed; recorded as an explicit gap."""


@dataclass
class ContextRecord:
    source_id: str
    source_kind: str
    native_id: str
    vintage: str               # revision id; new vintage => new record (never overwrite)
    availability_time_us: int  # when publicly knowable (may be later than event time)
    event_time_us: Optional[int]
    url: str
    parser_version: str
    payload: Dict[str, Any]

    def payload_sha256(self) -> str:
        text = json.dumps(self.payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(text.encode()).hexdigest()

    def to_row(self) -> Dict[str, Any]:
        d = asdict(self)
        d["payload_sha256"] = self.payload_sha256()
        return d


class ContextAdapter(ABC):
    source_id: str = "abstract"
    source_kind: str = "abstract"

    @abstractmethod
    def fetch(self, since_us: int, until_us: int) -> List[ContextRecord]:
        """Return context records with availability_time_us in [since_us, until_us].
        Implementations must raise AccessGap (not fabricate) if the source is unreachable."""
        raise NotImplementedError


class NotWiredAdapter(ContextAdapter):
    """Honest placeholder: declares the contract but is not wired to a live source."""

    def __init__(self, source_id: str, source_kind: str, reason: str):
        self.source_id = source_id
        self.source_kind = source_kind
        self.reason = reason

    def fetch(self, since_us: int, until_us: int) -> List[ContextRecord]:
        raise AccessGap(f"{self.source_id}: not wired this pass ({self.reason})")


_FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


class FixtureFOMCAdapter(ContextAdapter):
    """Working FOMC adapter backed by a local fixture, to exercise the contract offline.

    The fixture mimics the shape of FOMC meeting-date data from federalreserve.gov. A real
    network adapter would replace `_load()` but keep this exact record/provenance contract.
    """

    source_id = "fomc"
    source_kind = "fomc"
    url = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
    parser_version = "fixture_v1"

    def __init__(self, fixture_path: Optional[str] = None):
        self.fixture_path = fixture_path or os.path.join(_FIXTURE_DIR, "fomc_sample.json")

    def _load(self) -> List[Dict[str, Any]]:
        with open(self.fixture_path, "r", encoding="utf-8") as fh:
            return json.load(fh)

    def fetch(self, since_us: int, until_us: int) -> List[ContextRecord]:
        out: List[ContextRecord] = []
        for item in self._load():
            avail = int(item["availability_time_us"])
            if since_us <= avail <= until_us:
                out.append(ContextRecord(
                    source_id=self.source_id, source_kind=self.source_kind,
                    native_id=item["native_id"], vintage=item.get("vintage", "v1"),
                    availability_time_us=avail,
                    event_time_us=item.get("event_time_us"),
                    url=self.url, parser_version=self.parser_version,
                    payload=item["payload"]))
        return out


def _get_bytes(url: str, *, user_agent: str, timeout: int = 20) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": user_agent,
            "Accept": "application/json, application/xml, text/xml",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except Exception as exc:
        raise AccessGap(f"{url}: {type(exc).__name__}: {exc}") from exc


def _get_json(url: str, *, user_agent: str, timeout: int = 20) -> Dict[str, Any]:
    try:
        return json.loads(_get_bytes(url, user_agent=user_agent, timeout=timeout))
    except AccessGap:
        raise
    except Exception as exc:
        raise AccessGap(f"{url}: invalid JSON: {exc}") from exc


def _dt_us(value: str) -> int:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1_000_000)


class FederalReserveRSSAdapter(ContextAdapter):
    """Federal Reserve monetary-policy releases from the official RSS feed."""

    source_id = "fomc"
    source_kind = "fomc"
    url = "https://www.federalreserve.gov/feeds/press_monetary.xml"
    parser_version = "fed_rss_v1"

    def fetch(self, since_us: int, until_us: int) -> List[ContextRecord]:
        raw = _get_bytes(self.url, user_agent="IsaiahCryptoResearch/1.0")
        try:
            root = ET.fromstring(raw)
        except ET.ParseError as exc:
            raise AccessGap(f"Federal Reserve RSS parse error: {exc}") from exc
        out = []
        for item in root.findall(".//item"):
            pub_text = item.findtext("pubDate")
            if not pub_text:
                continue
            dt = parsedate_to_datetime(pub_text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            avail = int(dt.timestamp() * 1_000_000)
            if not (since_us <= avail <= until_us):
                continue
            link = (item.findtext("link") or self.url).strip()
            native_id = (item.findtext("guid") or link).strip()
            out.append(ContextRecord(
                source_id=self.source_id,
                source_kind=self.source_kind,
                native_id=native_id,
                vintage=dt.date().isoformat(),
                availability_time_us=avail,
                event_time_us=avail,
                url=link,
                parser_version=self.parser_version,
                payload={
                    "title": (item.findtext("title") or "").strip(),
                    "description": (item.findtext("description") or "").strip(),
                    "published": pub_text,
                    "link": link,
                },
            ))
        return out


class RSSContextAdapter(ContextAdapter):
    """Bounded RSS/Atom context with first-seen and revision provenance."""

    parser_version = "rss_context_v1"
    user_agent = "IsaiahCryptoResearch/1.0"
    url = ""

    def fetch(self, since_us: int, until_us: int) -> List[ContextRecord]:
        retrieved_us = int(time.time() * 1_000_000)
        raw = _get_bytes(self.url, user_agent=self.user_agent)
        try:
            root = ET.fromstring(raw)
        except ET.ParseError as exc:
            raise AccessGap(f"{self.source_id} RSS parse error: {exc}") from exc

        items = list(root.findall(".//item"))
        if not items:
            # Atom fallback used by some status-page providers.
            items = list(root.findall(".//{http://www.w3.org/2005/Atom}entry"))

        out: List[ContextRecord] = []
        for item in items:
            atom = item.tag.endswith("entry")
            if atom:
                ns = "{http://www.w3.org/2005/Atom}"
                title = (item.findtext(f"{ns}title") or "").strip()
                description = (
                    item.findtext(f"{ns}summary")
                    or item.findtext(f"{ns}content")
                    or ""
                ).strip()
                published = (
                    item.findtext(f"{ns}published")
                    or item.findtext(f"{ns}updated")
                    or ""
                ).strip()
                native_id = (item.findtext(f"{ns}id") or "").strip()
                link_node = item.find(f"{ns}link")
                link = (
                    link_node.attrib.get("href", "")
                    if link_node is not None
                    else ""
                ).strip()
                categories = [
                    node.attrib.get("term", "").strip()
                    for node in item.findall(f"{ns}category")
                    if node.attrib.get("term")
                ]
            else:
                title = (item.findtext("title") or "").strip()
                description = (item.findtext("description") or "").strip()
                published = (
                    item.findtext("pubDate")
                    or item.findtext("published")
                    or ""
                ).strip()
                link = (item.findtext("link") or "").strip()
                native_id = (item.findtext("guid") or link).strip()
                categories = [
                    (node.text or "").strip()
                    for node in item.findall("category")
                    if (node.text or "").strip()
                ]

            if not native_id:
                native_id = link or hashlib.sha256(
                    f"{title}|{published}".encode()
                ).hexdigest()
            try:
                dt = parsedate_to_datetime(published)
            except (TypeError, ValueError):
                try:
                    dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
                except (TypeError, ValueError):
                    dt = datetime.fromtimestamp(
                        retrieved_us / 1_000_000, tz=timezone.utc
                    )
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            avail = int(dt.timestamp() * 1_000_000)
            if not (since_us <= avail <= until_us):
                continue

            stable_payload = {
                "title": title,
                "description": description,
                "published": published,
                "link": link,
                "guid": native_id,
                "categories": categories,
            }
            revision = hashlib.sha256(
                json.dumps(
                    stable_payload, sort_keys=True, separators=(",", ":")
                ).encode()
            ).hexdigest()[:16]
            out.append(
                ContextRecord(
                    source_id=self.source_id,
                    source_kind=self.source_kind,
                    native_id=native_id,
                    vintage=revision,
                    availability_time_us=avail,
                    event_time_us=avail,
                    url=link or self.url,
                    parser_version=self.parser_version,
                    payload={
                        **stable_payload,
                        "publication_time_us": avail,
                        "first_seen_time_us": retrieved_us,
                        "availability_basis": "publisher_publication_time",
                        "revision_hash": revision,
                    },
                )
            )
        return out


class CoinbaseStatusRSSAdapter(RSSContextAdapter):
    """Official Coinbase incidents and maintenance updates."""

    source_id = "coinbase_official_status"
    source_kind = "official_exchange"
    url = "https://status.coinbase.com/history.rss"
    parser_version = "coinbase_status_rss_v1"


class CoinDeskRSSAdapter(RSSContextAdapter):
    """Attributed CoinDesk publication feed; archival context only."""

    source_id = "news_coindesk"
    source_kind = "attributed_news"
    url = "https://www.coindesk.com/arc/outboundfeeds/rss/"
    parser_version = "coindesk_rss_v1"


class BLSCPIAdapter(ContextAdapter):
    """Headline CPI-U observations from the official BLS Public Data API."""

    source_id = "cpi_bls"
    source_kind = "cpi_bls"
    series_id = "CUUR0000SA0"
    url = f"https://api.bls.gov/publicAPI/v2/timeseries/data/{series_id}"
    parser_version = "bls_cpi_v1"

    def fetch(self, since_us: int, until_us: int) -> List[ContextRecord]:
        retrieved_us = int(time.time() * 1_000_000)
        data = _get_json(self.url, user_agent="IsaiahCryptoResearch/1.0")
        if data.get("status") != "REQUEST_SUCCEEDED":
            raise AccessGap(f"BLS API failed: {data.get('message')}")
        out = []
        for series in data.get("Results", {}).get("series", []):
            for item in series.get("data", []):
                period = item.get("period", "")
                if not (period.startswith("M") and period[1:].isdigit()):
                    continue
                month = int(period[1:])
                if not 1 <= month <= 12:
                    continue
                event_us = _dt_us(
                    f"{int(item['year']):04d}-{month:02d}-01T00:00:00+00:00"
                )
                # BLS observations do not carry the release timestamp. Using
                # retrieval time is conservative and prevents historical revisions
                # from appearing available before this collector actually saw them.
                avail = retrieved_us
                if not (since_us <= avail <= until_us):
                    continue
                out.append(ContextRecord(
                    source_id=self.source_id,
                    source_kind=self.source_kind,
                    native_id=f"{self.series_id}:{item['year']}:{period}",
                    vintage=datetime.now(timezone.utc).date().isoformat(),
                    availability_time_us=avail,
                    event_time_us=event_us,
                    url=self.url,
                    parser_version=self.parser_version,
                    payload={
                        "series_id": self.series_id,
                        "year": item["year"],
                        "period": period,
                        "period_name": item.get("periodName"),
                        "value": item.get("value"),
                        "footnotes": item.get("footnotes", []),
                        "availability_basis": "retrieval_time_conservative",
                    },
                ))
        return out


class EdgarCoinbaseAdapter(ContextAdapter):
    """Coinbase Global filing metadata from SEC EDGAR submissions JSON."""

    source_id = "edgar_coinbase"
    source_kind = "edgar"
    cik = "0001679788"
    url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    parser_version = "sec_submissions_v1"

    def __init__(self, user_agent: str = "Isaiah crypto research isaiah@example.invalid"):
        self.user_agent = user_agent

    def fetch(self, since_us: int, until_us: int) -> List[ContextRecord]:
        data = _get_json(self.url, user_agent=self.user_agent)
        recent = data.get("filings", {}).get("recent", {})
        keys = (
            "accessionNumber", "filingDate", "reportDate", "acceptanceDateTime",
            "act", "form", "fileNumber", "filmNumber", "items", "size",
            "isXBRL", "isInlineXBRL", "primaryDocument",
            "primaryDocDescription",
        )
        count = len(recent.get("accessionNumber", []))
        out = []
        for i in range(count):
            row = {
                key: recent.get(key, [None] * count)[i]
                if i < len(recent.get(key, [])) else None
                for key in keys
            }
            accepted = row.get("acceptanceDateTime")
            if accepted:
                avail = _dt_us(str(accepted))
            else:
                avail = _dt_us(f"{row['filingDate']}T23:59:59+00:00")
            if not (since_us <= avail <= until_us):
                continue
            accession = str(row["accessionNumber"])
            filing_url = (
                "https://www.sec.gov/Archives/edgar/data/"
                f"{int(self.cik)}/{accession.replace('-', '')}/{row['primaryDocument']}"
            )
            out.append(ContextRecord(
                source_id=self.source_id,
                source_kind=self.source_kind,
                native_id=accession,
                vintage=str(row.get("acceptanceDateTime") or row.get("filingDate")),
                availability_time_us=avail,
                event_time_us=avail,
                url=filing_url,
                parser_version=self.parser_version,
                payload=row,
            ))
        return out


class CFTCBitcoinCOTAdapter(ContextAdapter):
    """Bitcoin and Micro Bitcoin Traders-in-Financial-Futures observations."""

    source_id = "cftc_bitcoin"
    source_kind = "cftc"
    parser_version = "cftc_tff_v1"

    def __init__(self, year: Optional[int] = None):
        self.year = year or datetime.now(timezone.utc).year
        self.url = (
            "https://www.cftc.gov/files/dea/history/"
            f"fut_fin_txt_{self.year}.zip"
        )

    def fetch(self, since_us: int, until_us: int) -> List[ContextRecord]:
        retrieved_us = int(time.time() * 1_000_000)
        if not (since_us <= retrieved_us <= until_us):
            return []
        raw = _get_bytes(self.url, user_agent="IsaiahCryptoResearch/1.0")
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                names = [
                    name for name in archive.namelist()
                    if name.lower().endswith(".txt")
                ]
                if not names:
                    raise AccessGap("CFTC archive contains no text report")
                text = archive.read(names[0]).decode("utf-8-sig", errors="replace")
        except (zipfile.BadZipFile, OSError) as exc:
            raise AccessGap(f"CFTC archive parse error: {exc}") from exc

        out = []
        for row in csv.DictReader(io.StringIO(text)):
            market = (row.get("Market_and_Exchange_Names") or "").strip()
            if "BITCOIN" not in market.upper():
                continue
            report_date = (row.get("Report_Date_as_YYYY-MM-DD") or "").strip()
            if not report_date:
                continue
            event_us = _dt_us(f"{report_date}T00:00:00+00:00")
            payload = {str(k).strip(): v.strip() if isinstance(v, str) else v
                       for k, v in row.items()}
            payload_hash = hashlib.sha256(
                json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            out.append(ContextRecord(
                source_id=self.source_id,
                source_kind=self.source_kind,
                native_id=f"{market}:{report_date}",
                vintage=payload_hash[:16],
                availability_time_us=retrieved_us,
                event_time_us=event_us,
                url=self.url,
                parser_version=self.parser_version,
                payload={
                    **payload,
                    "availability_basis": "retrieval_time_conservative",
                    "official_release_note": (
                        "COT is generally released Friday 15:30 ET for Tuesday positions"
                    ),
                },
            ))
        return out


class CoinbaseIntxDerivativesAdapter(ContextAdapter):
    """Public BTC perpetual open-interest and realized-funding observations."""

    source_id = "coinbase_intx_btc_perp"
    source_kind = "funding_oi"
    parser_version = "coinbase_intx_rest_v1"

    def __init__(self, product_id: str = "BTC-PERP"):
        self.product_id = product_id
        base = "https://api.international.coinbase.com/api/v1/instruments"
        self.instrument_url = f"{base}/{product_id}"
        self.funding_url = f"{base}/{product_id}/funding"
        self.url = self.instrument_url

    def fetch(self, since_us: int, until_us: int) -> List[ContextRecord]:
        retrieved_us = int(time.time() * 1_000_000)
        if not (since_us <= retrieved_us <= until_us):
            return []
        instrument = _get_json(
            self.instrument_url,
            user_agent="IsaiahCryptoResearch/1.0",
        )
        funding = _get_json(
            self.funding_url,
            user_agent="IsaiahCryptoResearch/1.0",
        )
        records = [
            ContextRecord(
                source_id=self.source_id,
                source_kind=self.source_kind,
                native_id=(
                    f"{self.product_id}:open_interest:"
                    f"{retrieved_us // 60_000_000}"
                ),
                vintage="v1",
                availability_time_us=retrieved_us,
                event_time_us=retrieved_us,
                url=self.instrument_url,
                parser_version=self.parser_version,
                payload={
                    "product_id": self.product_id,
                    "open_interest": instrument.get("open_interest"),
                    "qty_24hr": instrument.get("qty_24hr"),
                    "notional_24hr": instrument.get("notional_24hr"),
                    "trading_state": instrument.get("trading_state"),
                    "funding_interval": instrument.get("funding_interval"),
                    "availability_basis": "retrieval_time_conservative",
                },
            )
        ]
        for item in funding.get("results", []):
            event_time = item.get("event_time")
            if not event_time:
                continue
            event_us = _dt_us(str(event_time))
            payload = {
                "product_id": self.product_id,
                "instrument_id": item.get("instrument_id"),
                "funding_rate": item.get("funding_rate"),
                "mark_price": item.get("mark_price"),
                "event_time": event_time,
                "availability_basis": "retrieval_time_conservative",
            }
            vintage = hashlib.sha256(
                json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()[:16]
            records.append(ContextRecord(
                source_id=self.source_id,
                source_kind=self.source_kind,
                native_id=f"{self.product_id}:funding:{event_time}",
                vintage=vintage,
                availability_time_us=retrieved_us,
                event_time_us=event_us,
                url=self.funding_url,
                parser_version=self.parser_version,
                payload=payload,
            ))
        return records
