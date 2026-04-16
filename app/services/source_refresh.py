from __future__ import annotations

import ipaddress
import json
import re
import zipfile
from collections import Counter
from csv import DictReader
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from io import BytesIO, StringIO
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlparse
from urllib.request import Request, urlopen
from xml.etree import ElementTree

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models.entities import (
    ChatterItem,
    Headline,
    MacroObservation,
    MacroSeries,
    PriceObservation,
    PriceSeries,
    Source,
    SourceFetchRun,
)
from app.services.realized_outcomes import sync_realized_outcomes

EVDS_TEMPLATE_PREFIX = "evds-search:"
EVDS_DATAGROUPS_ENDPOINT = "https://evds3.tcmb.gov.tr/igmevdsms-dis/getCategorywithDatagroups?type=json"
EVDS_SERIE_LIST_ENDPOINTS = (
    "https://evds2.tcmb.gov.tr/service/evds/serieList/type=json&code={datagroup_code}",
    "https://evds3.tcmb.gov.tr/service/evds/serieList/type=json&code={datagroup_code}",
)
CBRT_POLICY_RATE_CODE = "CBRT_POLICY_RATE_1W_REPO"
CBRT_IRFCL_WEEKLY_ZIP_TEXT = "zip link"
CBRT_IRFCL_ROW_LABELS = {
    "CBRT_IRFCL_OFFICIAL_RESERVE_ASSETS": "I.A Official reserve assets",
    "CBRT_IRFCL_FX_RESERVES": "I.A.1 Foreign currency reserves (in convertible foreign currencies)",
}


def _utc_now_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


@dataclass(frozen=True)
class FeedEntry:
    title: str
    link: str | None
    summary: str | None
    published_at: datetime
    author: str | None


@dataclass(frozen=True)
class FetchOutcome:
    status: str
    items_ingested: int
    error_message: str | None = None


@dataclass(frozen=True)
class MacroObservationPoint:
    observation_date: datetime
    value: float
    release_date: datetime | None = None
    notes: str | None = None


@dataclass(frozen=True)
class MacroSeriesFetchResult:
    raw_payload: str
    observations: list[MacroObservationPoint]


@dataclass(frozen=True)
class EVDSResolvedSeries:
    template_code: str
    datagroup_code: str
    datagroup_title: str
    series_code: str
    series_name: str


@dataclass(frozen=True)
class PriceObservationPoint:
    observed_at: datetime
    close_value: float
    open_value: float | None = None
    high_value: float | None = None
    low_value: float | None = None
    volume: float | None = None


@dataclass(frozen=True)
class PriceSeriesFetchResult:
    raw_payload: str
    observations: list[PriceObservationPoint]


class _VisibleTextHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        cleaned = data.strip()
        if cleaned:
            self.parts.append(unescape(cleaned))

    def text(self) -> str:
        return "\n".join(self.parts)


class _AnchorCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.anchors: list[tuple[str, str]] = []
        self._current_href: str | None = None
        self._current_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() != "a":
            return
        attributes = {key.casefold(): value for key, value in attrs}
        self._current_href = attributes.get("href")
        self._current_text = []

    def handle_data(self, data: str) -> None:
        if self._current_href is None:
            return
        cleaned = data.strip()
        if cleaned:
            self._current_text.append(unescape(cleaned))

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() != "a" or self._current_href is None:
            return
        anchor_text = " ".join(self._current_text).strip()
        self.anchors.append((self._current_href, anchor_text))
        self._current_href = None
        self._current_text = []


def execute_queued_refreshes(session: Session, settings: Settings) -> dict:
    queued_runs = list(
        session.scalars(
            select(SourceFetchRun)
            .where(SourceFetchRun.status == "queued")
            .order_by(SourceFetchRun.started_at.asc(), SourceFetchRun.id.asc())
        ).all()
    )
    if not queued_runs:
        return {
            "processed_runs": 0,
            "status_counts": {},
            "message": "No queued refreshes were available.",
        }

    status_counts: Counter[str] = Counter()
    price_refresh_touched = False
    for run in queued_runs:
        source = session.get(Source, run.source_id)
        if source is None:
            run.status = "error"
            run.finished_at = _utc_now_naive()
            run.error_message = "Source record no longer exists."
            status_counts[run.status] += 1
            continue

        outcome = _run_source_refresh(session, settings, source, run)
        run.status = outcome.status
        run.items_ingested = outcome.items_ingested
        run.error_message = outcome.error_message
        run.finished_at = _utc_now_naive()
        status_counts[outcome.status] += 1
        if source.category == "market_prices" and outcome.status in {"success", "partial-success"}:
            price_refresh_touched = True

    if price_refresh_touched:
        sync_realized_outcomes(session)

    session.commit()
    return {
        "processed_runs": len(queued_runs),
        "status_counts": dict(status_counts),
        "message": _format_refresh_summary(len(queued_runs), status_counts),
    }


def _run_source_refresh(
    session: Session,
    settings: Settings,
    source: Source,
    run: SourceFetchRun,
) -> FetchOutcome:
    if source.parser_adapter in {"fred", "imf", "evds", "cbrt_policy_rate", "cbrt_irfcl"}:
        return _refresh_macro_source(session, settings, source, run)
    if source.parser_adapter in {"ecb_exr", "cboe_vix_csv"}:
        return _refresh_price_source(session, settings, source, run)
    if source.parser_adapter == "rss":
        return _refresh_headlines(session, settings, source, run)
    if source.parser_adapter == "chatter":
        return _refresh_chatter(session, settings, source, run)
    if source.requires_credentials:
        return FetchOutcome(
            status="credentials-required",
            items_ingested=0,
            error_message="This source needs credentials before live refresh can run.",
        )
    return FetchOutcome(
        status="config-required",
        items_ingested=0,
        error_message="This source adapter still needs a series or endpoint configuration.",
    )


def _refresh_macro_source(
    session: Session,
    settings: Settings,
    source: Source,
    run: SourceFetchRun,
) -> FetchOutcome:
    series_list = list(
        session.scalars(
            select(MacroSeries)
            .where(MacroSeries.source_id == source.id)
            .order_by(MacroSeries.name.asc(), MacroSeries.id.asc())
        ).all()
    )
    if not series_list:
        return FetchOutcome(
            status="config-required",
            items_ingested=0,
            error_message="No macro series are configured for this source yet.",
        )
    if source.parser_adapter == "evds" and not settings.evds_api_key:
        return FetchOutcome(
            status="credentials-required",
            items_ingested=0,
            error_message="Set FX_EVDS_API_KEY before refreshing EVDS series.",
        )

    items_written = 0
    series_with_data = 0
    errors: list[str] = []
    for series in series_list:
        result = _fetch_macro_series(settings, source, series)
        if isinstance(result, FetchOutcome):
            errors.append(f"{series.code}: {result.error_message or result.status}")
            continue

        series_with_data += 1
        items_written += _store_macro_points(session, series, result.observations)
        _write_raw_payload(
            settings,
            source.slug,
            run.id,
            result.raw_payload,
            "json",
            suffix=series.code,
        )

    if series_with_data == 0:
        return FetchOutcome(
            status="error",
            items_ingested=0,
            error_message="; ".join(errors[:3]) or "Macro refresh failed.",
        )
    if errors:
        return FetchOutcome(
            status="partial-success",
            items_ingested=items_written,
            error_message="; ".join(errors[:3]),
        )
    return FetchOutcome(status="success", items_ingested=items_written)


def _refresh_headlines(
    session: Session,
    settings: Settings,
    source: Source,
    run: SourceFetchRun,
) -> FetchOutcome:
    fetched_feed = _fetch_feed_entries(
        source,
        timeout_seconds=settings.source_fetch_timeout_seconds,
        max_bytes=settings.source_fetch_max_bytes,
    )
    if isinstance(fetched_feed, FetchOutcome):
        return fetched_feed

    raw_payload, entries = fetched_feed
    _write_raw_payload(settings, source.slug, run.id, raw_payload, "xml")

    inserted = 0
    for entry in entries:
        if _headline_exists(session, source.id, entry):
            continue
        session.add(
            Headline(
                source_id=source.id,
                published_at=entry.published_at,
                title=entry.title,
                url=entry.link,
                summary=entry.summary,
                sentiment_hint=None,
                tags=["news", source.slug],
            )
        )
        inserted += 1
    session.flush()
    return FetchOutcome(status="success", items_ingested=inserted)


def _refresh_price_source(
    session: Session,
    settings: Settings,
    source: Source,
    run: SourceFetchRun,
) -> FetchOutcome:
    series_list = list(
        session.scalars(
            select(PriceSeries)
            .where(PriceSeries.source_id == source.id)
            .order_by(PriceSeries.name.asc(), PriceSeries.id.asc())
        ).all()
    )
    if not series_list:
        return FetchOutcome(
            status="config-required",
            items_ingested=0,
            error_message="No price series are configured for this source yet.",
        )

    items_written = 0
    series_with_data = 0
    errors: list[str] = []
    for series in series_list:
        result = _fetch_price_series(settings, source, series)
        if isinstance(result, FetchOutcome):
            errors.append(f"{series.symbol}: {result.error_message or result.status}")
            continue

        series_with_data += 1
        items_written += _store_price_points(session, series, result.observations)
        _write_raw_payload(
            settings,
            source.slug,
            run.id,
            result.raw_payload,
            "csv",
            suffix=series.symbol,
        )

    if series_with_data == 0:
        return FetchOutcome(
            status="error",
            items_ingested=0,
            error_message="; ".join(errors[:3]) or "Price refresh failed.",
        )
    if errors:
        return FetchOutcome(
            status="partial-success",
            items_ingested=items_written,
            error_message="; ".join(errors[:3]),
        )
    return FetchOutcome(status="success", items_ingested=items_written)


def _refresh_chatter(
    session: Session,
    settings: Settings,
    source: Source,
    run: SourceFetchRun,
) -> FetchOutcome:
    fetched_feed = _fetch_feed_entries(
        source,
        timeout_seconds=settings.source_fetch_timeout_seconds,
        max_bytes=settings.source_fetch_max_bytes,
    )
    if isinstance(fetched_feed, FetchOutcome):
        return fetched_feed

    raw_payload, entries = fetched_feed
    _write_raw_payload(settings, source.slug, run.id, raw_payload, "xml")

    inserted = 0
    for entry in entries:
        if _chatter_exists(session, source.id, entry):
            continue
        session.add(
            ChatterItem(
                source_id=source.id,
                posted_at=entry.published_at,
                author=entry.author,
                content=entry.title,
                url=entry.link,
                trust_score=0.35,
                tags=["social", source.slug],
            )
        )
        inserted += 1
    session.flush()
    return FetchOutcome(status="success", items_ingested=inserted)


def _fetch_feed_entries(
    source: Source,
    timeout_seconds: int,
    max_bytes: int,
) -> tuple[str, list[FeedEntry]] | FetchOutcome:
    if not source.endpoint:
        return FetchOutcome(
            status="config-required",
            items_ingested=0,
            error_message="No feed endpoint is configured for this source.",
        )

    try:
        xml_text = _fetch_text(
            source.endpoint,
            timeout_seconds=timeout_seconds,
            max_bytes=max_bytes,
        )
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        return FetchOutcome(status="error", items_ingested=0, error_message=str(exc))

    try:
        return xml_text, _parse_rss_entries(xml_text)
    except ElementTree.ParseError as exc:
        return FetchOutcome(
            status="error",
            items_ingested=0,
            error_message=f"Feed parsing failed: {exc}",
        )


def _fetch_macro_series(
    settings: Settings,
    source: Source,
    series: MacroSeries,
) -> MacroSeriesFetchResult | FetchOutcome:
    if source.parser_adapter == "fred":
        return _fetch_fred_series(settings, series)
    if source.parser_adapter == "imf":
        return _fetch_imf_series(settings, series)
    if source.parser_adapter == "evds":
        return _fetch_evds_series(settings, series)
    if source.parser_adapter == "cbrt_policy_rate":
        return _fetch_cbrt_policy_rate_series(settings, source, series)
    if source.parser_adapter == "cbrt_irfcl":
        return _fetch_cbrt_irfcl_series(settings, source, series)
    return FetchOutcome(
        status="config-required",
        items_ingested=0,
        error_message=f"Unsupported macro adapter: {source.parser_adapter}",
    )


def _fetch_price_series(
    settings: Settings,
    source: Source,
    series: PriceSeries,
) -> PriceSeriesFetchResult | FetchOutcome:
    if source.parser_adapter == "ecb_exr":
        return _fetch_ecb_exr_series(settings, series)
    if source.parser_adapter == "cboe_vix_csv":
        return _fetch_cboe_vix_series(settings, source, series)
    return FetchOutcome(
        status="config-required",
        items_ingested=0,
        error_message=f"Unsupported price adapter: {source.parser_adapter}",
    )


def _fetch_fred_series(
    settings: Settings,
    series: MacroSeries,
) -> MacroSeriesFetchResult | FetchOutcome:
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={quote(series.code)}"
    try:
        raw_payload = _fetch_text(
            url,
            timeout_seconds=settings.source_fetch_timeout_seconds,
            max_bytes=settings.source_fetch_max_bytes,
            extra_headers={
                "User-Agent": None,
                "Accept": "text/csv, */*",
            },
        )
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        return FetchOutcome(status="error", items_ingested=0, error_message=str(exc))

    points: list[MacroObservationPoint] = []
    for observation in DictReader(StringIO(raw_payload)):
        value = _parse_float(
            observation.get(series.code)
            or observation.get(series.code.upper())
            or observation.get(series.code.lower())
        )
        observation_date = _parse_observation_date(
            observation.get("observation_date")
            or observation.get("DATE")
            or observation.get("date")
        )
        if value is None or observation_date is None:
            continue
        points.append(
            MacroObservationPoint(
                observation_date=observation_date,
                value=value,
            )
        )
    if not points:
        return FetchOutcome(
            status="error",
            items_ingested=0,
            error_message=f"No FRED observations found for {series.code}.",
        )
    return MacroSeriesFetchResult(raw_payload=raw_payload, observations=points)


def _fetch_imf_series(
    settings: Settings,
    series: MacroSeries,
) -> MacroSeriesFetchResult | FetchOutcome:
    raw_parts = [part for part in series.code.split("/") if part]
    if not raw_parts:
        return FetchOutcome(
            status="config-required",
            items_ingested=0,
            error_message="IMF series code cannot be empty.",
        )
    indicator = raw_parts[0]
    url_parts = [quote(part) for part in raw_parts]
    url = "https://www.imf.org/external/datamapper/api/v1/" + "/".join(url_parts)
    try:
        raw_payload = _fetch_text(
            url,
            timeout_seconds=settings.source_fetch_timeout_seconds,
            max_bytes=settings.source_fetch_max_bytes,
            extra_headers={
                "User-Agent": None,
                "Accept": "application/json, text/plain, */*",
            },
        )
        payload = json.loads(raw_payload)
    except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        return FetchOutcome(status="error", items_ingested=0, error_message=str(exc))

    values_root = payload.get("values", {})
    indicator_values = values_root.get(indicator)
    if indicator_values is None and len(values_root) == 1:
        indicator_values = next(iter(values_root.values()))

    data_node = indicator_values
    for part in raw_parts[1:]:
        if not isinstance(data_node, dict):
            data_node = None
            break
        data_node = data_node.get(part)

    if not isinstance(data_node, dict):
        return FetchOutcome(
            status="error",
            items_ingested=0,
            error_message=f"No IMF observations found for {series.code}.",
        )

    points = [
        MacroObservationPoint(
            observation_date=datetime(int(year), 1, 1),
            value=value,
        )
        for year, value in _iter_year_value_pairs(data_node)
    ]
    if not points:
        return FetchOutcome(
            status="error",
            items_ingested=0,
            error_message=f"No IMF observations found for {series.code}.",
        )
    return MacroSeriesFetchResult(raw_payload=raw_payload, observations=points)


def _fetch_cbrt_policy_rate_series(
    settings: Settings,
    source: Source,
    series: MacroSeries,
) -> MacroSeriesFetchResult | FetchOutcome:
    if series.code != CBRT_POLICY_RATE_CODE:
        return FetchOutcome(
            status="config-required",
            items_ingested=0,
            error_message=f"Unsupported CBRT policy-rate series: {series.code}",
        )
    try:
        raw_payload = _fetch_text(
            source.endpoint or "",
            timeout_seconds=settings.source_fetch_timeout_seconds,
            max_bytes=settings.source_fetch_max_bytes,
        )
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        return FetchOutcome(status="error", items_ingested=0, error_message=str(exc))

    points = _parse_cbrt_policy_rate_points(raw_payload)
    if not points:
        return FetchOutcome(
            status="error",
            items_ingested=0,
            error_message="No CBRT one-week repo policy-rate observations were found.",
        )
    payload = json.dumps(
        {
            "source_url": source.endpoint,
            "observation_count": len(points),
            "first_observation_date": points[0].observation_date.strftime("%Y-%m-%d"),
            "latest_observation_date": points[-1].observation_date.strftime("%Y-%m-%d"),
            "latest_value": points[-1].value,
            "observations": _serialize_macro_points(points),
        }
    )
    return MacroSeriesFetchResult(raw_payload=payload, observations=points)


def _fetch_cbrt_irfcl_series(
    settings: Settings,
    source: Source,
    series: MacroSeries,
) -> MacroSeriesFetchResult | FetchOutcome:
    target_label = CBRT_IRFCL_ROW_LABELS.get(series.code)
    if not target_label:
        return FetchOutcome(
            status="config-required",
            items_ingested=0,
            error_message=f"Unsupported CBRT reserve series: {series.code}",
        )
    try:
        raw_page = _fetch_text(
            source.endpoint or "",
            timeout_seconds=settings.source_fetch_timeout_seconds,
            max_bytes=settings.source_fetch_max_bytes,
        )
        zip_url = _extract_cbrt_irfcl_zip_url(raw_page, source.endpoint or "")
        zip_bytes = _fetch_bytes(
            zip_url,
            timeout_seconds=settings.source_fetch_timeout_seconds,
            max_bytes=settings.source_fetch_max_bytes * 4,
        )
    except (HTTPError, URLError, TimeoutError, ValueError, KeyError, zipfile.BadZipFile) as exc:
        return FetchOutcome(status="error", items_ingested=0, error_message=str(exc))

    parsed = _parse_cbrt_irfcl_points(zip_bytes, target_label)
    if parsed is None:
        return FetchOutcome(
            status="error",
            items_ingested=0,
            error_message=f"No CBRT reserve observation was found for {target_label}.",
        )
    points, workbook_entry = parsed
    raw_payload = json.dumps(
        {
            "page_url": source.endpoint,
            "zip_url": zip_url,
            "workbook_entry": workbook_entry,
            "target_label": target_label,
            "observation_count": len(points),
            "first_observation_date": points[0].observation_date.strftime("%Y-%m-%d"),
            "latest_observation_date": points[-1].observation_date.strftime("%Y-%m-%d"),
            "latest_value": points[-1].value,
            "observations": _serialize_macro_points(points),
        }
    )
    return MacroSeriesFetchResult(raw_payload=raw_payload, observations=points)


def _fetch_evds_series(
    settings: Settings,
    series: MacroSeries,
) -> MacroSeriesFetchResult | FetchOutcome:
    resolved_series: EVDSResolvedSeries | None = None
    requested_code = series.code
    extracted_code = series.code
    if series.code.startswith(EVDS_TEMPLATE_PREFIX):
        resolved = _resolve_evds_series_template(settings, series.code)
        if isinstance(resolved, FetchOutcome):
            return resolved
        resolved_series = resolved
        requested_code = resolved.series_code
        extracted_code = resolved.series_code

    end_date = datetime.now().strftime("%d-%m-%Y")
    url = (
        "https://evds2.tcmb.gov.tr/service/evds/series="
        f"{quote(requested_code)}&startDate=01-01-2000&endDate={end_date}&type=json"
    )
    try:
        raw_payload = _fetch_text(
            url,
            timeout_seconds=settings.source_fetch_timeout_seconds,
            max_bytes=settings.source_fetch_max_bytes,
            extra_headers={"key": settings.evds_api_key or ""},
        )
        payload = json.loads(raw_payload)
    except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        return FetchOutcome(status="error", items_ingested=0, error_message=str(exc))

    rows = _extract_evds_rows(payload)
    if not rows:
        return FetchOutcome(
            status="error",
            items_ingested=0,
            error_message=f"No EVDS observations found for {series.code}.",
        )

    points: list[MacroObservationPoint] = []
    for row in rows:
        observation_date = _parse_observation_date(
            row.get("Tarih")
            or row.get("DATE")
            or row.get("date")
            or row.get("Date")
        )
        value = _extract_evds_value(row, extracted_code)
        if observation_date is None or value is None:
            continue
        points.append(
            MacroObservationPoint(
                observation_date=observation_date,
                value=value,
            )
        )
    if not points:
        return FetchOutcome(
            status="error",
            items_ingested=0,
            error_message=f"No EVDS observations found for {series.code}.",
        )
    if resolved_series is not None:
        raw_payload = json.dumps(
            {
                "template_resolution": {
                    "template_code": resolved_series.template_code,
                    "datagroup_code": resolved_series.datagroup_code,
                    "datagroup_title": resolved_series.datagroup_title,
                    "series_code": resolved_series.series_code,
                    "series_name": resolved_series.series_name,
                },
                "observations": payload,
            }
        )
    return MacroSeriesFetchResult(raw_payload=raw_payload, observations=points)


def _resolve_evds_series_template(
    settings: Settings,
    template_code: str,
) -> EVDSResolvedSeries | FetchOutcome:
    try:
        datagroup_query, series_query = _parse_evds_template_code(template_code)
    except ValueError as exc:
        return FetchOutcome(status="error", items_ingested=0, error_message=str(exc))

    headers = {
        "key": settings.evds_api_key or "",
        "Accept": "application/json, text/plain, */*",
    }
    try:
        datagroup_payload = _fetch_json_payload(
            EVDS_DATAGROUPS_ENDPOINT,
            timeout_seconds=settings.source_fetch_timeout_seconds,
            max_bytes=settings.source_fetch_max_bytes,
            extra_headers=headers,
        )
    except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        return FetchOutcome(status="error", items_ingested=0, error_message=str(exc))

    datagroup_rows = _extract_evds_candidate_rows(
        datagroup_payload,
        code_candidates=(
            "code",
            "datagroupcode",
            "datagroup_code",
            "datagrupkodu",
            "groupcode",
            "group_code",
        ),
        label_candidates=(
            "name",
            "title",
            "datagroupname",
            "datagroup_name",
            "datagrupadi",
            "datagrupadieng",
            "text",
        ),
    )
    datagroup_row = _pick_best_evds_row(
        datagroup_rows,
        query=datagroup_query,
        code_candidates=(
            "code",
            "datagroupcode",
            "datagroup_code",
            "datagrupkodu",
            "groupcode",
            "group_code",
        ),
        label_candidates=(
            "name",
            "title",
            "datagroupname",
            "datagroup_name",
            "datagrupadi",
            "datagrupadieng",
            "text",
        ),
        object_label="EVDS data group",
    )
    if isinstance(datagroup_row, FetchOutcome):
        return datagroup_row

    datagroup_code = _evds_row_value(
        datagroup_row,
        "code",
        "datagroupcode",
        "datagroup_code",
        "datagrupkodu",
        "groupcode",
        "group_code",
    )
    datagroup_title = _evds_row_value(
        datagroup_row,
        "name",
        "title",
        "datagroupname",
        "datagroup_name",
        "datagrupadi",
        "datagrupadieng",
        "text",
    )
    if not datagroup_code or not datagroup_title:
        return FetchOutcome(
            status="error",
            items_ingested=0,
            error_message=f"EVDS data-group metadata was incomplete for {template_code}.",
        )

    series_rows: list[dict] = []
    last_error: str | None = None
    for endpoint in EVDS_SERIE_LIST_ENDPOINTS:
        url = endpoint.format(datagroup_code=quote(datagroup_code))
        try:
            series_payload = _fetch_json_payload(
                url,
                timeout_seconds=settings.source_fetch_timeout_seconds,
                max_bytes=settings.source_fetch_max_bytes,
                extra_headers=headers,
            )
        except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
            last_error = str(exc)
            continue
        series_rows = _extract_evds_candidate_rows(
            series_payload,
            code_candidates=(
                "code",
                "seriescode",
                "series_code",
                "seriecode",
                "seriekodu",
                "seri_kodu",
                "kod",
            ),
            label_candidates=(
                "name",
                "seriename",
                "seriesname",
                "series_name",
                "seriadi",
                "seri_adi",
                "title",
                "text",
            ),
        )
        if series_rows:
            break
    if not series_rows:
        message = last_error or f"No EVDS series catalog rows were found for {datagroup_code}."
        return FetchOutcome(status="error", items_ingested=0, error_message=message)

    series_row = _pick_best_evds_row(
        series_rows,
        query=series_query or datagroup_query,
        code_candidates=(
            "code",
            "seriescode",
            "series_code",
            "seriecode",
            "seriekodu",
            "seri_kodu",
            "kod",
        ),
        label_candidates=(
            "name",
            "seriename",
            "seriesname",
            "series_name",
            "seriadi",
            "seri_adi",
            "title",
            "text",
        ),
        object_label="EVDS series",
    )
    if isinstance(series_row, FetchOutcome):
        return series_row

    series_code = _evds_row_value(
        series_row,
        "code",
        "seriescode",
        "series_code",
        "seriecode",
        "seriekodu",
        "seri_kodu",
        "kod",
    )
    series_name = _evds_row_value(
        series_row,
        "name",
        "seriename",
        "seriesname",
        "series_name",
        "seriadi",
        "seri_adi",
        "title",
        "text",
    )
    if not series_code or not series_name:
        return FetchOutcome(
            status="error",
            items_ingested=0,
            error_message=f"EVDS series metadata was incomplete for {template_code}.",
        )
    return EVDSResolvedSeries(
        template_code=template_code,
        datagroup_code=datagroup_code,
        datagroup_title=datagroup_title,
        series_code=series_code,
        series_name=series_name,
    )


def _fetch_ecb_exr_series(
    settings: Settings,
    series: PriceSeries,
) -> PriceSeriesFetchResult | FetchOutcome:
    url = (
        "https://data-api.ecb.europa.eu/service/data/EXR/"
        f"{quote(series.symbol, safe='.')}"
        "?lastNObservations=30&format=csvdata"
    )
    try:
        raw_payload = _fetch_text(
            url,
            timeout_seconds=settings.source_fetch_timeout_seconds,
            max_bytes=settings.source_fetch_max_bytes,
        )
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        return FetchOutcome(status="error", items_ingested=0, error_message=str(exc))

    rows = list(DictReader(StringIO(raw_payload)))
    if not rows:
        return FetchOutcome(
            status="error",
            items_ingested=0,
            error_message=f"No ECB EXR rows found for {series.symbol}.",
        )

    points: list[PriceObservationPoint] = []
    for row in rows:
        observed_at = _parse_observation_date(
            row.get("TIME_PERIOD")
            or row.get("time_period")
            or row.get("TIME PERIOD")
        )
        close_value = _parse_float(
            row.get("OBS_VALUE")
            or row.get("obs_value")
            or row.get("OBS VALUE")
        )
        if observed_at is None or close_value is None:
            continue
        points.append(
            PriceObservationPoint(
                observed_at=observed_at,
                close_value=close_value,
            )
        )
    if not points:
        return FetchOutcome(
            status="error",
            items_ingested=0,
            error_message=f"No ECB EXR observations found for {series.symbol}.",
        )
    return PriceSeriesFetchResult(raw_payload=raw_payload, observations=points)


def _fetch_cboe_vix_series(
    settings: Settings,
    source: Source,
    series: PriceSeries,
) -> PriceSeriesFetchResult | FetchOutcome:
    series_endpoint = _cboe_series_endpoint(source, series.symbol)
    if not series_endpoint:
        return FetchOutcome(
            status="config-required",
            items_ingested=0,
            error_message="No Cboe volatility endpoint is configured.",
        )
    try:
        raw_payload = _fetch_text(
            series_endpoint,
            timeout_seconds=settings.source_fetch_timeout_seconds,
            max_bytes=settings.source_fetch_max_bytes,
        )
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        return FetchOutcome(status="error", items_ingested=0, error_message=str(exc))

    rows = list(DictReader(StringIO(raw_payload)))
    if not rows:
        return FetchOutcome(
            status="error",
            items_ingested=0,
            error_message=f"No Cboe volatility rows found for {series.symbol}.",
        )

    points: list[PriceObservationPoint] = []
    for row in rows:
        observed_at = _parse_observation_date(row.get("DATE") or row.get("Date"))
        close_value = _extract_cboe_close_value(row, series.symbol)
        if observed_at is None or close_value is None:
            continue
        points.append(
            PriceObservationPoint(
                observed_at=observed_at,
                open_value=_parse_float(row.get("OPEN") or row.get("Open")),
                high_value=_parse_float(row.get("HIGH") or row.get("High")),
                low_value=_parse_float(row.get("LOW") or row.get("Low")),
                close_value=close_value,
            )
        )
    if not points:
        return FetchOutcome(
            status="error",
            items_ingested=0,
            error_message=f"No Cboe volatility observations found for {series.symbol}.",
        )
    return PriceSeriesFetchResult(raw_payload=raw_payload, observations=points)


def _extract_cboe_close_value(row: dict[str, str | None], symbol: str) -> float | None:
    for key in ("CLOSE", "Close", symbol.upper(), symbol.title(), symbol.lower()):
        value = _parse_float(row.get(key))
        if value is not None:
            return value
    return None


def _cboe_series_endpoint(source: Source, symbol: str) -> str | None:
    if not symbol:
        return None
    base_endpoint = (source.endpoint or "").strip()
    if not base_endpoint:
        return None
    sanitized_symbol = quote(symbol.strip().upper(), safe="")
    if base_endpoint.endswith(".csv"):
        prefix = base_endpoint.rsplit("/", 1)[0]
        return f"{prefix}/{sanitized_symbol}_History.csv"
    return f"{base_endpoint.rstrip('/')}/{sanitized_symbol}_History.csv"


def _fetch_json_payload(
    url: str,
    timeout_seconds: int,
    max_bytes: int,
    extra_headers: dict[str, str] | None = None,
) -> object:
    raw_payload = _fetch_text(
        url,
        timeout_seconds=timeout_seconds,
        max_bytes=max_bytes,
        extra_headers=extra_headers,
    )
    return json.loads(raw_payload)


def _fetch_bytes(
    url: str,
    timeout_seconds: int = 15,
    max_bytes: int = 2_000_000,
    extra_headers: dict[str, str] | None = None,
) -> bytes:
    _validate_remote_url(url)
    headers = {
        "User-Agent": "FXTRYRiskLab/0.1 (+local research tool)",
        "Accept": "*/*",
    }
    if extra_headers:
        for key, value in extra_headers.items():
            if value is None:
                headers.pop(key, None)
                continue
            headers[key] = value
    request = Request(url, headers=headers)
    with urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310
        _validate_remote_url(response.geturl())
        payload = response.read(max_bytes + 1)
    if len(payload) > max_bytes:
        raise ValueError(f"Remote payload exceeded {max_bytes} bytes.")
    return payload


def _fetch_text(
    url: str,
    timeout_seconds: int = 15,
    max_bytes: int = 2_000_000,
    extra_headers: dict[str, str] | None = None,
) -> str:
    payload = _fetch_bytes(
        url,
        timeout_seconds=timeout_seconds,
        max_bytes=max_bytes,
        extra_headers=extra_headers,
    )
    return payload.decode("utf-8", errors="replace")


def _validate_remote_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ValueError("Only HTTPS source fetches are allowed.")
    if parsed.username or parsed.password:
        raise ValueError("Embedded credentials are not allowed in source URLs.")
    if not parsed.hostname:
        raise ValueError("Source URL must include a hostname.")
    hostname = parsed.hostname.casefold()
    if hostname in {"localhost", "127.0.0.1", "::1"} or hostname.endswith(".local"):
        raise ValueError("Local or loopback hosts are not allowed for source fetches.")
    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        return
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
        raise ValueError("Private or local IP ranges are not allowed for source fetches.")


def _parse_rss_entries(xml_text: str) -> list[FeedEntry]:
    root = ElementTree.fromstring(xml_text)
    items = root.findall("./channel/item")
    if not items:
        items = root.findall(".//item")

    entries: list[FeedEntry] = []
    for item in items[:25]:
        title = _clean_xml_text(item.findtext("title")) or "Untitled entry"
        link = _clean_xml_text(item.findtext("link"))
        summary = _clean_xml_text(item.findtext("description"))
        author = _clean_xml_text(item.findtext("author"))
        published_raw = _clean_xml_text(item.findtext("pubDate"))
        published_at = _parse_feed_datetime(published_raw)
        entries.append(
            FeedEntry(
                title=title,
                link=link,
                summary=summary,
                published_at=published_at,
                author=author,
            )
        )
    return entries


def _parse_feed_datetime(value: str | None) -> datetime:
    if not value:
        return _utc_now_naive()
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return _utc_now_naive()
    if parsed.tzinfo is None:
        return parsed
    return parsed.astimezone(UTC).replace(tzinfo=None)


def _clean_xml_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = unescape(value).strip()
    return cleaned or None


def _parse_cbrt_policy_rate_points(html_text: str) -> list[MacroObservationPoint]:
    parser = _VisibleTextHTMLParser()
    parser.feed(html_text)
    text = parser.text()
    matches = re.findall(
        r"(?P<date>\d{2}\.\d{2}\.\d{4})\s*-\s*(?P<rate>\d{1,2}(?:[.,]\d{1,2})?)",
        text,
    )
    points_by_date: dict[datetime, MacroObservationPoint] = {}
    for date_text, rate_text in matches:
        observation_date = _parse_observation_date(date_text)
        value = _parse_float(rate_text)
        if observation_date is None or value is None:
            continue
        points_by_date[observation_date] = MacroObservationPoint(
            observation_date=observation_date,
            release_date=observation_date,
            value=value,
            notes="CBRT 1 Week Repo page",
        )
    return sorted(points_by_date.values(), key=lambda item: item.observation_date)


def _extract_cbrt_irfcl_zip_url(html_text: str, base_url: str) -> str:
    parser = _AnchorCollector()
    parser.feed(html_text)
    for href, anchor_text in parser.anchors:
        normalized_text = anchor_text.casefold()
        if CBRT_IRFCL_WEEKLY_ZIP_TEXT not in normalized_text:
            continue
        if ".zip" not in href.casefold():
            continue
        return urljoin(base_url, href)
    for href, _anchor_text in parser.anchors:
        if ".zip" in href.casefold():
            return urljoin(base_url, href)
    raise ValueError("No CBRT reserve ZIP link was found on the IRFCL page.")


def _parse_cbrt_irfcl_points(
    zip_bytes: bytes,
    target_label: str,
) -> tuple[list[MacroObservationPoint], str] | None:
    with zipfile.ZipFile(BytesIO(zip_bytes)) as outer_zip:
        workbook_entry = next(
            (
                entry.filename
                for entry in outer_zip.infolist()
                if entry.filename.casefold().endswith(".xlsx")
            ),
            None,
        )
        if workbook_entry is None:
            raise KeyError("CBRT reserve ZIP did not contain an XLSX workbook.")
        workbook_bytes = outer_zip.read(workbook_entry)

    with zipfile.ZipFile(BytesIO(workbook_bytes)) as workbook_zip:
        shared_strings = _xlsx_shared_strings(workbook_zip)
        sheet_rows = _xlsx_sheet_rows(workbook_zip, shared_strings)

    header_row = sheet_rows.get(9)
    target_row_number = next(
        (
            row_number
            for row_number, row_values in sheet_rows.items()
            if _normalize_match_key(row_values.get("B", "")) == _normalize_match_key(target_label)
        ),
        None,
    )
    if not header_row or target_row_number is None:
        return None

    dated_columns = _irfcl_weekly_columns(header_row)
    if not dated_columns:
        return None

    row_values = sheet_rows[target_row_number]
    points: list[MacroObservationPoint] = []
    for column, observation_date in dated_columns:
        value = _parse_float(row_values.get(column))
        if value is None:
            continue
        points.append(
            MacroObservationPoint(
                observation_date=observation_date,
                release_date=observation_date,
                value=value,
                notes=target_label,
            )
        )
    if not points:
        return None
    return points, workbook_entry


def _xlsx_shared_strings(workbook_zip: zipfile.ZipFile) -> list[str]:
    shared_strings_entry = workbook_zip.read("xl/sharedStrings.xml")
    root = ElementTree.fromstring(shared_strings_entry)
    namespace = {"a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    values: list[str] = []
    for item in root.findall("a:si", namespace):
        text = "".join(node.text or "" for node in item.iterfind(".//a:t", namespace))
        values.append(text)
    return values


def _xlsx_sheet_rows(
    workbook_zip: zipfile.ZipFile,
    shared_strings: list[str],
) -> dict[int, dict[str, str]]:
    sheet_xml = workbook_zip.read("xl/worksheets/sheet1.xml")
    root = ElementTree.fromstring(sheet_xml)
    namespace = {"a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    rows: dict[int, dict[str, str]] = {}
    for row in root.findall(".//a:sheetData/a:row", namespace):
        row_number = int(row.attrib["r"])
        cell_values: dict[str, str] = {}
        for cell in row.findall("a:c", namespace):
            reference = cell.attrib.get("r", "")
            column = "".join(character for character in reference if character.isalpha())
            raw_value = cell.findtext("a:v", default="", namespaces=namespace)
            if not column or raw_value == "":
                continue
            if cell.attrib.get("t") == "s":
                value = shared_strings[int(raw_value)]
            else:
                value = raw_value
            cell_values[column] = value
        rows[row_number] = cell_values
    return rows


def _irfcl_weekly_columns(header_row: dict[str, str]) -> list[tuple[str, datetime]]:
    candidates: list[tuple[str, datetime]] = []
    for column, value in header_row.items():
        observation_date = _parse_irfcl_header_date(value)
        if observation_date is None:
            continue
        candidates.append((column, observation_date))
    candidates.sort(key=lambda item: item[1])
    return candidates


def _serialize_macro_points(
    points: list[MacroObservationPoint],
) -> list[dict[str, str | float | None]]:
    return [
        {
            "observation_date": point.observation_date.strftime("%Y-%m-%d"),
            "release_date": (
                point.release_date.strftime("%Y-%m-%d")
                if point.release_date is not None
                else None
            ),
            "value": point.value,
            "notes": point.notes,
        }
        for point in points
    ]


def _parse_irfcl_header_date(value: str | None) -> datetime | None:
    if not value:
        return None
    direct_date = _parse_observation_date(value)
    if direct_date is not None:
        return direct_date
    numeric_value = _parse_float(value)
    if numeric_value is None:
        return None
    return datetime(1899, 12, 30) + timedelta(days=numeric_value)


def _parse_observation_date(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d.%m.%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(normalized, fmt)
        except ValueError:
            continue
    if len(normalized) == 4 and normalized.isdigit():
        return datetime(int(normalized), 1, 1)
    return None


def _parse_float(value: object) -> float | None:
    if value in (None, "", ".", "NA", "N/A", "-"):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        normalized = value.strip().replace(",", ".")
        try:
            return float(normalized)
        except ValueError:
            return None
    return None


def _iter_year_value_pairs(node: dict) -> list[tuple[str, float]]:
    pairs: list[tuple[str, float]] = []
    for year, raw_value in node.items():
        value = _parse_float(raw_value)
        if value is None:
            continue
        year_text = str(year)
        if year_text.isdigit():
            pairs.append((year_text, value))
    pairs.sort(key=lambda pair: pair[0])
    return pairs


def _parse_evds_template_code(template_code: str) -> tuple[str, str | None]:
    raw_body = template_code[len(EVDS_TEMPLATE_PREFIX) :].strip()
    if not raw_body:
        raise ValueError("EVDS template codes must include an official data-group title.")
    datagroup_title, separator, series_hint = raw_body.partition("|")
    datagroup_title = datagroup_title.strip()
    series_hint = series_hint.strip() if separator else ""
    if not datagroup_title:
        raise ValueError("EVDS template codes must include an official data-group title.")
    if separator and not series_hint:
        raise ValueError("EVDS template codes must keep the optional series hint non-empty.")
    return datagroup_title, series_hint or None


def _extract_evds_candidate_rows(
    payload: object,
    *,
    code_candidates: tuple[str, ...],
    label_candidates: tuple[str, ...],
) -> list[dict]:
    rows: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for row in _iter_nested_dict_rows(payload):
        code = _evds_row_value(row, *code_candidates)
        label = _evds_row_value(row, *label_candidates)
        if not code or not label:
            continue
        signature = (code, label)
        if signature in seen:
            continue
        seen.add(signature)
        rows.append(row)
    return rows


def _iter_nested_dict_rows(payload: object):
    if isinstance(payload, dict):
        yield payload
        for value in payload.values():
            yield from _iter_nested_dict_rows(value)
    elif isinstance(payload, list):
        for item in payload:
            yield from _iter_nested_dict_rows(item)


def _pick_best_evds_row(
    rows: list[dict],
    *,
    query: str,
    code_candidates: tuple[str, ...],
    label_candidates: tuple[str, ...],
    object_label: str,
) -> dict | FetchOutcome:
    if not rows:
        return FetchOutcome(
            status="error",
            items_ingested=0,
            error_message=f"No {object_label.lower()} rows were available for EVDS matching.",
        )

    scored_rows: list[tuple[int, str, str, dict]] = []
    for row in rows:
        code = _evds_row_value(row, *code_candidates)
        label = _evds_row_value(row, *label_candidates)
        if not code or not label:
            continue
        score = _evds_match_score(query, label)
        if score <= 0:
            continue
        scored_rows.append((score, label, code, row))
    if not scored_rows:
        return FetchOutcome(
            status="error",
            items_ingested=0,
            error_message=f"No {object_label.lower()} matched '{query}'.",
        )

    scored_rows.sort(key=lambda item: (-item[0], len(item[1]), item[1].casefold(), item[2]))
    best_score = scored_rows[0][0]
    best_rows = [item for item in scored_rows if item[0] == best_score]
    if len(best_rows) > 1:
        exact_matches = [
            item
            for item in best_rows
            if _normalize_match_key(item[1]) == _normalize_match_key(query)
        ]
        if len(exact_matches) == 1:
            return exact_matches[0][3]
        choices = ", ".join(item[1] for item in best_rows[:3])
        return FetchOutcome(
            status="error",
            items_ingested=0,
            error_message=(
                f"EVDS {object_label.lower()} lookup for '{query}' was ambiguous. "
                f"Top matches: {choices}."
            ),
        )
    return best_rows[0][3]


def _extract_evds_rows(payload: object) -> list[dict]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for value in payload.values():
            if isinstance(value, list) and value and isinstance(value[0], dict):
                return value
    return []


def _extract_evds_value(row: dict, series_code: str) -> float | None:
    candidate_keys = [series_code, series_code.replace(".", "_"), series_code.upper()]
    for key in candidate_keys:
        value = _parse_float(row.get(key))
        if value is not None:
            return value
    for key, raw_value in row.items():
        if str(key).casefold() in {"tarih", "date"}:
            continue
        value = _parse_float(raw_value)
        if value is not None:
            return value
    return None


def _evds_row_value(row: dict, *candidate_keys: str) -> str | None:
    normalized_candidates = {_normalize_lookup_key(key) for key in candidate_keys}
    for key, raw_value in row.items():
        if _normalize_lookup_key(key) not in normalized_candidates:
            continue
        if isinstance(raw_value, (dict, list)):
            continue
        text = str(raw_value).strip()
        if text:
            return text
    return None


def _normalize_lookup_key(value: object) -> str:
    return "".join(character for character in str(value).casefold() if character.isalnum())


def _normalize_match_key(value: str) -> str:
    tokens: list[str] = []
    current: list[str] = []
    for character in value.casefold():
        if character.isalnum():
            current.append(character)
            continue
        if current:
            tokens.append("".join(current))
            current = []
    if current:
        tokens.append("".join(current))
    return " ".join(tokens)


def _evds_match_score(query: str, candidate: str) -> int:
    normalized_query = _normalize_match_key(query)
    normalized_candidate = _normalize_match_key(candidate)
    if not normalized_query or not normalized_candidate:
        return 0
    if normalized_query == normalized_candidate:
        return 1000
    if normalized_query in normalized_candidate:
        return 900 - abs(len(normalized_candidate) - len(normalized_query))
    if normalized_candidate in normalized_query:
        return 850 - abs(len(normalized_query) - len(normalized_candidate))

    query_tokens = set(normalized_query.split())
    candidate_tokens = set(normalized_candidate.split())
    overlap = query_tokens & candidate_tokens
    if not overlap:
        return 0
    return int(
        len(overlap) * 100
        + (len(overlap) / max(len(query_tokens), 1)) * 75
        + (len(overlap) / max(len(candidate_tokens), 1)) * 50
    )


def _headline_exists(session: Session, source_id: int, entry: FeedEntry) -> bool:
    existing = session.scalar(
        select(Headline.id)
        .where(
            Headline.source_id == source_id,
            Headline.title == entry.title,
            Headline.published_at == entry.published_at,
        )
        .limit(1)
    )
    return existing is not None


def _chatter_exists(session: Session, source_id: int, entry: FeedEntry) -> bool:
    existing = session.scalar(
        select(ChatterItem.id)
        .where(
            ChatterItem.source_id == source_id,
            ChatterItem.content == entry.title,
            ChatterItem.posted_at == entry.published_at,
        )
        .limit(1)
    )
    return existing is not None


def _store_macro_points(
    session: Session,
    series: MacroSeries,
    observations: list[MacroObservationPoint],
) -> int:
    writes = 0
    for point in observations:
        existing = session.scalar(
            select(MacroObservation)
            .where(
                MacroObservation.series_id == series.id,
                MacroObservation.observation_date == point.observation_date,
            )
            .limit(1)
        )
        if existing is None:
            session.add(
                MacroObservation(
                    series_id=series.id,
                    observation_date=point.observation_date,
                    release_date=point.release_date,
                    value=point.value,
                    notes=point.notes,
                )
            )
            writes += 1
            continue
        if (
            existing.value != point.value
            or existing.release_date != point.release_date
            or existing.notes != point.notes
        ):
            existing.value = point.value
            existing.release_date = point.release_date
            existing.notes = point.notes
            writes += 1
    session.flush()
    return writes


def _store_price_points(
    session: Session,
    series: PriceSeries,
    observations: list[PriceObservationPoint],
) -> int:
    writes = 0
    for point in observations:
        existing = session.scalar(
            select(PriceObservation)
            .where(
                PriceObservation.series_id == series.id,
                PriceObservation.observed_at == point.observed_at,
            )
            .limit(1)
        )
        if existing is None:
            session.add(
                PriceObservation(
                    series_id=series.id,
                    observed_at=point.observed_at,
                    open_value=point.open_value,
                    high_value=point.high_value,
                    low_value=point.low_value,
                    close_value=point.close_value,
                    volume=point.volume,
                )
            )
            writes += 1
            continue
        if (
            existing.open_value != point.open_value
            or existing.high_value != point.high_value
            or existing.low_value != point.low_value
            or existing.close_value != point.close_value
            or existing.volume != point.volume
        ):
            existing.open_value = point.open_value
            existing.high_value = point.high_value
            existing.low_value = point.low_value
            existing.close_value = point.close_value
            existing.volume = point.volume
            writes += 1
    session.flush()
    return writes


def _write_raw_payload(
    settings: Settings,
    source_slug: str,
    run_id: int,
    payload: str,
    extension: str,
    suffix: str | None = None,
) -> None:
    output_dir = Path(settings.raw_data_dir) / source_slug
    output_dir.mkdir(parents=True, exist_ok=True)
    file_name = f"run-{run_id:05d}"
    if suffix:
        safe_suffix = "".join(
            character if character.isalnum() or character in {"-", "_"} else "_"
            for character in suffix
        ).strip("_")
        if safe_suffix:
            file_name += f"-{safe_suffix}"
    output_path = output_dir / f"{file_name}.{extension}"
    output_path.write_text(payload, encoding="utf-8")


def _format_refresh_summary(run_count: int, status_counts: Counter[str]) -> str:
    parts = [f"Processed {run_count} queued refreshes."]
    if status_counts:
        parts.append(
            " | ".join(
                f"{status}: {count}"
                for status, count in sorted(status_counts.items())
            )
        )
    return " ".join(parts)
