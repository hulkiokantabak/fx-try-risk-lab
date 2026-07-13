from __future__ import annotations

import json
import re
import sys
import zipfile
from csv import DictReader
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from io import BytesIO, StringIO
from pathlib import Path
from statistics import mean
from time import sleep
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin
from urllib.request import Request, urlopen
from xml.etree import ElementTree


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from risklab.forecast import MODEL_VERSION, build_empirical_forecast
from risklab.ledger import ledger_summary, update_forecast_ledger
from risklab.quality import (
    age_days,
    checksum,
    ensure_fresh,
    iso_utc,
    item_count,
    latest_observation,
    validate_feed,
    validate_series,
    validate_series_map,
)


DOCS_DIR = ROOT / "docs"
DATA_DIR = DOCS_DIR / "data"
LATEST_PATH = DATA_DIR / "latest.json"
HISTORY_PATH = DATA_DIR / "history.json"
SOURCE_CACHE_PATH = DATA_DIR / "source_cache.json"
FORECAST_LEDGER_PATH = DATA_DIR / "forecast_ledger.json"

THRESHOLDS = {
    "1w": 2,
    "1m": 5,
    "3m": 10,
    "6m": 15,
    "1y": 25,
}

ECB_SERIES = {
    "EURTRY": "D.TRY.EUR.SP00.A",
    "EURUSD": "D.USD.EUR.SP00.A",
    "EURZAR": "D.ZAR.EUR.SP00.A",
    "EURBRL": "D.BRL.EUR.SP00.A",
    "EURHUF": "D.HUF.EUR.SP00.A",
    "EURPLN": "D.PLN.EUR.SP00.A",
}

FRED_SERIES = {
    "FEDFUNDS": "Effective Federal Funds Rate",
    "DGS10": "US 10-Year Treasury Yield",
    "DGS2": "US 2-Year Treasury Yield",
    "DTWEXBGS": "Broad Trade-Weighted US Dollar Index",
}

CBOE_SERIES = {
    "VIX": "Cboe VIX",
    "VIX9D": "Cboe VIX9D",
    "VVIX": "Cboe VVIX",
    "VXEEM": "Cboe VXEEM",
    "OVX": "Cboe OVX",
    "GVZ": "Cboe GVZ",
}

GOOGLE_RSS_URL = (
    "https://news.google.com/rss/search?"
    "q=(Turkish+lira+OR+CBRT+OR+Turkey+inflation)&hl=en-US&gl=US&ceid=US:en"
)
REDDIT_RSS_URL = (
    "https://www.reddit.com/search.rss?"
    "q=(Turkish%20lira%20OR%20USDTRY%20OR%20CBRT)&sort=new"
)
CBRT_POLICY_RATE_URL = (
    "https://www.tcmb.gov.tr/wps/wcm/connect/EN/TCMB%2BEN/Main%2BMenu/"
    "Core%2BFunctions/Monetary%2BPolicy/Central%2BBank%2BInterest%2BRates/1%2BWeek%2BRepo"
)
CBRT_RESERVES_URL = (
    "https://www.tcmb.gov.tr/wps/wcm/connect/EN/TCMB%2BEN/Main%2BMenu/Statistics/"
    "Balance%2Bof%2BPayments%2Band%2BRelated%2BStatistics/International%2BReserves"
    "%2Band%2BForeign%2BCurrency%2BLiquidity/"
)
CBRT_IRFCL_WEEKLY_ZIP_TEXT = "zip link"
CBRT_IRFCL_ROW_LABELS = {
    "official_reserve_assets": "I.A Official reserve assets",
    "fx_reserves": "I.A.1 Foreign currency reserves (in convertible foreign currencies)",
}

SOURCE_RULES = {
    "ecb_eurtry": (lambda value: validate_series(value, minimum_count=750, positive=True), 10),
    "ecb_eurusd": (lambda value: validate_series(value, minimum_count=750, positive=True), 10),
    "ecb_eurzar": (lambda value: validate_series(value, minimum_count=750, positive=True), 10),
    "ecb_eurbrl": (lambda value: validate_series(value, minimum_count=750, positive=True), 10),
    "ecb_eurhuf": (lambda value: validate_series(value, minimum_count=750, positive=True), 10),
    "ecb_eurpln": (lambda value: validate_series(value, minimum_count=750, positive=True), 10),
    "fred_fedfunds": (
        lambda value: validate_series(value, minimum_count=50, plausible_range=(-1.0, 30.0)),
        50,
    ),
    "fred_dgs10": (lambda value: validate_series(value, minimum_count=100, plausible_range=(-5.0, 30.0)), 14),
    "fred_dgs2": (lambda value: validate_series(value, minimum_count=100, plausible_range=(-5.0, 30.0)), 14),
    "fred_dtwexbgs": (lambda value: validate_series(value, minimum_count=100, positive=True), 14),
    "cbrt_policy_rate": (
        lambda value: validate_series(value, minimum_count=2, plausible_range=(0.0, 100.0)),
        75,
    ),
    "cbrt_reserves": (
        lambda value: validate_series_map(
            value,
            required_keys=("official_reserve_assets", "fx_reserves"),
            minimum_count=5,
        ),
        45,
    ),
    "google_news_rss": (lambda value: validate_feed(value, minimum_count=1), 14),
    "reddit_rss": (lambda value: validate_feed(value, minimum_count=1), 14),
}
for _symbol in CBOE_SERIES:
    SOURCE_RULES[f"cboe_{_symbol.casefold()}"] = (
        lambda value: validate_series(value, minimum_count=100, positive=True),
        10,
    )


@dataclass(frozen=True)
class SeriesPoint:
    observed_at: datetime
    value: float


@dataclass(frozen=True)
class FeedEntry:
    title: str
    link: str | None
    published_at: datetime


class VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        cleaned = data.strip()
        if cleaned:
            self.parts.append(unescape(cleaned))

    def text(self) -> str:
        return "\n".join(self.parts)


class AnchorCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.anchors: list[tuple[str, str]] = []
        self._href: str | None = None
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() != "a":
            return
        attr_map = {key.casefold(): value for key, value in attrs}
        self._href = attr_map.get("href")
        self._parts = []

    def handle_data(self, data: str) -> None:
        if self._href is None:
            return
        cleaned = data.strip()
        if cleaned:
            self._parts.append(unescape(cleaned))

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() != "a" or self._href is None:
            return
        self.anchors.append((self._href, " ".join(self._parts).strip()))
        self._href = None
        self._parts = []


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    source_cache = load_source_cache()
    snapshot = build_snapshot(source_cache)
    ledger = snapshot.pop("_ledger_payload")
    history = update_history(snapshot)
    snapshot.setdefault("charts", {})["score_history"] = build_score_history_chart(history)
    source_cache["updated_at"] = snapshot["generated_at"]
    atomic_write_json(LATEST_PATH, snapshot)
    atomic_write_json(HISTORY_PATH, history)
    atomic_write_json(SOURCE_CACHE_PATH, source_cache)
    # Commit the append-only live record last, after every published dependency
    # has serialized successfully.
    atomic_write_json(FORECAST_LEDGER_PATH, ledger)
    print(f"Wrote {LATEST_PATH}")
    print(f"Wrote {HISTORY_PATH}")
    print(f"Wrote {SOURCE_CACHE_PATH}")
    print(f"Wrote {FORECAST_LEDGER_PATH}")


def build_snapshot(source_cache: dict[str, object]) -> dict:
    generated_at = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    warnings: list[str] = []
    source_health: dict[str, dict] = {}

    eur_try = try_fetch(
        "ECB EUR/TRY",
        lambda: fetch_ecb_series(ECB_SERIES["EURTRY"]),
        [],
        warnings,
        source_cache,
        "ecb_eurtry",
        serialize_series_long,
        deserialize_series,
    )
    eur_usd = try_fetch(
        "ECB EUR/USD",
        lambda: fetch_ecb_series(ECB_SERIES["EURUSD"]),
        [],
        warnings,
        source_cache,
        "ecb_eurusd",
        serialize_series_long,
        deserialize_series,
    )
    peer_pairs = {
        "USD/ZAR": derive_usd_cross(
            try_fetch(
                "ECB EUR/ZAR",
                lambda: fetch_ecb_series(ECB_SERIES["EURZAR"], last_n=800),
                [],
                warnings,
                source_cache,
                "ecb_eurzar",
                serialize_series,
                deserialize_series,
            ),
            eur_usd,
        ),
        "USD/BRL": derive_usd_cross(
            try_fetch(
                "ECB EUR/BRL",
                lambda: fetch_ecb_series(ECB_SERIES["EURBRL"], last_n=800),
                [],
                warnings,
                source_cache,
                "ecb_eurbrl",
                serialize_series,
                deserialize_series,
            ),
            eur_usd,
        ),
        "USD/HUF": derive_usd_cross(
            try_fetch(
                "ECB EUR/HUF",
                lambda: fetch_ecb_series(ECB_SERIES["EURHUF"], last_n=800),
                [],
                warnings,
                source_cache,
                "ecb_eurhuf",
                serialize_series,
                deserialize_series,
            ),
            eur_usd,
        ),
        "USD/PLN": derive_usd_cross(
            try_fetch(
                "ECB EUR/PLN",
                lambda: fetch_ecb_series(ECB_SERIES["EURPLN"], last_n=800),
                [],
                warnings,
                source_cache,
                "ecb_eurpln",
                serialize_series,
                deserialize_series,
            ),
            eur_usd,
        ),
    }
    usd_try = derive_usd_cross(eur_try, eur_usd)

    fred = {
        code: try_fetch(
            f"FRED {code}",
            lambda code=code: fetch_fred_series(code),
            [],
            warnings,
            source_cache,
            f"fred_{code.casefold()}",
            serialize_series,
            deserialize_series,
        )
        for code in FRED_SERIES
    }
    cboe = {
        symbol: try_fetch(
            f"CBOE {symbol}",
            lambda symbol=symbol: fetch_cboe_series(symbol),
            [],
            warnings,
            source_cache,
            f"cboe_{symbol.casefold()}",
            serialize_series,
            deserialize_series,
        )
        for symbol in CBOE_SERIES
    }
    policy_rate = try_fetch(
        "CBRT policy rate",
        fetch_cbrt_policy_rate,
        [],
        warnings,
        source_cache,
        "cbrt_policy_rate",
        serialize_series,
        deserialize_series,
    )
    reserves = try_fetch(
        "CBRT reserves",
        fetch_cbrt_reserves,
        {"official_reserve_assets": [], "fx_reserves": []},
        warnings,
        source_cache,
        "cbrt_reserves",
        serialize_series_map,
        deserialize_series_map,
    )
    headlines = try_fetch(
        "Google News RSS",
        lambda: fetch_rss_entries(GOOGLE_RSS_URL),
        [],
        warnings,
        source_cache,
        "google_news_rss",
        serialize_feed,
        deserialize_feed,
    )
    chatter = try_fetch(
        "Reddit RSS",
        lambda: fetch_rss_entries(REDDIT_RSS_URL),
        [],
        warnings,
        source_cache,
        "reddit_rss",
        serialize_feed,
        deserialize_feed,
    )

    market = build_market_section(usd_try, peer_pairs, cboe)
    macro = build_macro_section(fred, policy_rate, reserves)
    news = build_news_section(
        headlines,
        chatter,
        headline_available=source_is_usable(source_cache, "google_news_rss"),
        chatter_available=source_is_usable(source_cache, "reddit_rss"),
    )
    forecast = build_empirical_forecast(usd_try, THRESHOLDS)
    curve = {
        horizon: specification["probability"]
        for horizon, specification in forecast["horizons"].items()
    }
    uncertainty = {
        horizon: specification["uncertainty"]
        for horizon, specification in forecast["horizons"].items()
    }
    primary_horizon = "1m"
    primary_score = curve[primary_horizon]
    ledger, forecast_id = update_forecast_ledger(
        FORECAST_LEDGER_PATH,
        forecast,
        usd_try,
        issued_at=generated_at,
        persist=False,
    )
    data_health = build_data_health(source_cache, warnings)
    calibration_samples = [
        specification["sample"]["calibration_examples"]
        for specification in forecast["horizons"].values()
    ]
    calibration_status = "calibrated" if forecast["model"]["is_calibrated"] else "experimental"
    calibration = {
        "status": calibration_status,
        "method": "as-of local reliability shrinkage on resolved walk-forward forecasts",
        "sample_size": min(calibration_samples),
        "horizons": forecast["backtest"]["metrics"],
        **forecast["backtest"],
    }
    briefing = build_briefing(primary_horizon, primary_score, market, macro, news, warnings)
    why_read = build_why_read(market, macro, news, briefing)
    trigger_cards = build_trigger_cards(market, macro, news)
    history_entry = {
        "as_of": generated_at,
        "primary_horizon": primary_horizon,
        "primary_score": primary_score,
        "curve": curve,
        "market_regime": market["regime_label"],
        "macro_regime": macro["regime_label"],
        "headline": briefing["house_call"],
        "stance": briefing["stance"],
        "confidence": briefing["confidence"],
        "forecast_id": forecast_id,
        "model_version": MODEL_VERSION,
        "data_cutoff": forecast["data_cutoff"],
    }
    return {
        "schema_version": "2.0",
        "forecast_id": forecast_id,
        "generated_at": generated_at,
        "data_cutoff": forecast["data_cutoff"],
        "primary_horizon": primary_horizon,
        "thresholds": THRESHOLDS,
        "curve": curve,
        "uncertainty": uncertainty,
        "primary_score": primary_score,
        "headline": briefing["house_call"],
        "briefing": briefing,
        "summary": build_summary(primary_score, market, macro, news, briefing),
        "why_read": why_read,
        "trigger_cards": trigger_cards,
        "charts": {
            "market_trend": build_market_trend_chart(usd_try, peer_pairs),
        },
        "market": market,
        "macro": macro,
        "news": news,
        "reasons": build_reasons(market, macro, news),
        "watchlist": build_watchlist(market, macro, news),
        "warnings": warnings,
        "history_entry": history_entry,
        "model": forecast["model"],
        "baseline": forecast["baseline"],
        "target": forecast["target"],
        "event_definition": forecast["event_definition"],
        "forecast": forecast,
        "calibration": calibration,
        "signed_drivers": forecast["signed_drivers"],
        "data_health": data_health,
        "source_freshness": {
            source["key"]: {
                "status": source["status"],
                "latest_observation": source.get("latest_observation"),
                "age_days": source.get("age_days"),
                "stale_after_days": source.get("stale_after_days"),
            }
            for source in data_health["sources"]
        },
        "track_record": {
            "live_ledger": ledger_summary(ledger),
            "backtest": forecast["backtest"],
            "warning": "Backtest results are not live outcomes; the append-only ledger is the live record.",
        },
        "_ledger_payload": ledger,
    }


def atomic_write_json(path: Path, payload: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    temporary.replace(path)


def try_fetch(
    label: str,
    fetcher,
    fallback,
    warnings: list[str],
    source_cache: dict[str, object] | None = None,
    cache_key: str | None = None,
    serializer=None,
    deserializer=None,
):
    attempted_at = iso_utc()
    sources = source_cache.setdefault("sources", {}) if source_cache is not None else {}
    rule = SOURCE_RULES.get(cache_key or "")
    validator = rule[0] if rule is not None else (lambda _value: None)
    maximum_age_days = rule[1] if rule is not None else 30
    try:
        value = fetcher()
        validator(value)
        ensure_fresh(value, maximum_age_days=maximum_age_days)
        if source_cache is not None and cache_key and serializer is not None:
            payload = serializer(value)
            observed_at = latest_observation(value)
            sources[cache_key] = {
                "label": label,
                "payload": payload,
                "fetched_at": attempted_at,
                "last_attempt_at": attempted_at,
                "latest_observation": iso_utc(observed_at) if observed_at is not None else None,
                "item_count": item_count(payload),
                "checksum_sha256": checksum(payload),
                "status": "fresh",
                "used_cache": False,
                "stale_after_days": maximum_age_days,
                "last_error": None,
            }
        return value
    except Exception as exc:  # noqa: BLE001
        failure_message = f"{label}: {exc}"
        entry = sources.get(cache_key) if cache_key else None
        if isinstance(entry, dict) and "payload" in entry and deserializer is not None:
            try:
                cached = deserializer(entry["payload"])
                validator(cached)
                ensure_fresh(cached, maximum_age_days=maximum_age_days)
            except Exception as cache_exc:  # noqa: BLE001
                failure_message += f"; cached fallback also failed: {cache_exc}"
                entry["status"] = "unavailable"
                entry["used_cache"] = False
                entry["last_attempt_at"] = attempted_at
                entry["last_error"] = str(exc)
            else:
                warnings.append(f"{label}: {exc}; using cached last-good data")
                entry["status"] = "cached_fallback"
                entry["used_cache"] = True
                entry["last_attempt_at"] = attempted_at
                entry["last_error"] = str(exc)
                return cached
        warnings.append(failure_message)
        if source_cache is not None and cache_key:
            if not isinstance(entry, dict):
                entry = {
                    "label": label,
                    "payload": None,
                    "fetched_at": None,
                    "latest_observation": None,
                    "item_count": 0,
                    "checksum_sha256": None,
                }
                sources[cache_key] = entry
            entry.update(
                {
                    "status": "unavailable",
                    "used_cache": False,
                    "last_attempt_at": attempted_at,
                    "stale_after_days": maximum_age_days,
                    "last_error": str(exc),
                }
            )
        return fallback


def source_is_usable(source_cache: dict[str, object], key: str) -> bool:
    sources = source_cache.get("sources", {})
    if not isinstance(sources, dict):
        return False
    entry = sources.get(key)
    return isinstance(entry, dict) and entry.get("status") in {"fresh", "cached_fallback"}


def build_market_section(
    usd_try: list[SeriesPoint],
    peer_pairs: dict[str, list[SeriesPoint]],
    cboe: dict[str, list[SeriesPoint]],
) -> dict:
    usd_try_latest = latest_point(usd_try)
    usd_try_5d = percent_change(usd_try, 5)
    usd_try_20d = percent_change(usd_try, 20)
    peer_changes_20d = {
        label: percent_change(points, 20)
        for label, points in peer_pairs.items()
    }
    valid_peer_changes = [value for value in peer_changes_20d.values() if value is not None]
    peer_avg_20d = mean(valid_peer_changes) if valid_peer_changes else None
    try_gap_20d = safe_subtract(usd_try_20d, peer_avg_20d)

    vix = latest_value(cboe["VIX"])
    vxeem = latest_value(cboe["VXEEM"])
    vvix = latest_value(cboe["VVIX"])
    ovx = latest_value(cboe["OVX"])
    gvz = latest_value(cboe["GVZ"])

    market_pressure = mean_available(
        [
            score_scale(usd_try_5d, -1.5, 4.0),
            score_scale(usd_try_20d, -2.0, 10.0),
            score_scale(try_gap_20d, -3.0, 8.0),
        ]
    )
    volatility_pressure = mean_available(
        [
            score_scale(vix, 12.0, 35.0),
            score_scale(vxeem, 18.0, 40.0),
            score_scale(vvix, 80.0, 150.0),
            mean_available([score_scale(ovx, 20.0, 60.0), score_scale(gvz, 10.0, 30.0)]),
        ]
    )
    if market_pressure is None:
        regime_label = "Market-pressure data unavailable"
    elif market_pressure >= 66:
        regime_label = "TRY under acute market pressure"
    elif market_pressure >= 48:
        regime_label = "Broad EM stress with TRY vulnerability"
    else:
        regime_label = "Market pressure contained"

    return {
        "regime_label": regime_label,
        "usd_try": {
            "latest": round_or_none(usd_try_latest.value if usd_try_latest else None, 4),
            "date": format_date(usd_try_latest.observed_at if usd_try_latest else None),
            "change_5d": round_or_none(usd_try_5d, 3),
            "change_20d": round_or_none(usd_try_20d, 3),
        },
        "peer_avg_20d": round_or_none(peer_avg_20d, 3),
        "try_gap_20d": round_or_none(try_gap_20d, 3),
        "peers": [
            {
                "label": label,
                "change_20d": round_or_none(change, 3),
                "latest": round_or_none(latest_value(points), 4),
            }
            for label, points in peer_pairs.items()
            for change in [peer_changes_20d[label]]
        ],
        "volatility": {
            "VIX": round_or_none(vix, 2),
            "VXEEM": round_or_none(vxeem, 2),
            "VVIX": round_or_none(vvix, 2),
            "OVX": round_or_none(ovx, 2),
            "GVZ": round_or_none(gvz, 2),
        },
        "scores": {
            "market_pressure": round_or_none(market_pressure, 1),
            "volatility_pressure": round_or_none(volatility_pressure, 1),
        },
    }


def build_macro_section(
    fred: dict[str, list[SeriesPoint]],
    policy_rate: list[SeriesPoint],
    reserves: dict[str, list[SeriesPoint]],
) -> dict:
    dxy_change_20d = percent_change(fred["DTWEXBGS"], 20)
    fed_funds = latest_value(fred["FEDFUNDS"])
    us10y = latest_value(fred["DGS10"])
    us2y = latest_value(fred["DGS2"])
    policy_latest = latest_value(policy_rate)
    official_reserves_latest = latest_value(reserves["official_reserve_assets"])
    fx_reserves_latest = latest_value(reserves["fx_reserves"])
    official_reserves_change = percent_change_window(reserves["official_reserve_assets"], 4)
    fx_reserves_change = percent_change_window(reserves["fx_reserves"], 4)

    global_pressure = mean_available(
        [
            score_scale(dxy_change_20d, -1.0, 4.0),
            score_scale(us2y, 2.0, 5.5),
            score_scale(fed_funds, 2.0, 5.5),
        ]
    )
    policy_support_score = score_scale(policy_latest, 10.0, 50.0)
    domestic_pressure = mean_available(
        [
            100.0 - policy_support_score if policy_support_score is not None else None,
            score_scale(negative_or_zero(official_reserves_change), 0.0, 12.0),
            score_scale(negative_or_zero(fx_reserves_change), 0.0, 12.0),
        ]
    )

    policy_available = policy_latest is not None
    reserves_available = official_reserves_change is not None or fx_reserves_change is not None

    if not policy_available and not reserves_available:
        regime_label = "Domestic-policy and reserve data unavailable"
    elif policy_available and not reserves_available:
        regime_label = "Policy-rate signal available; reserve trend unavailable"
    elif reserves_available and not policy_available:
        regime_label = "Reserve trend available; policy rate unavailable"
    elif domestic_pressure >= 66:
        regime_label = "Domestic cushion is thin"
    elif domestic_pressure >= 48:
        regime_label = "Domestic support is mixed"
    else:
        regime_label = "Domestic policy and reserves are supportive"
    return {
        "regime_label": regime_label,
        "global": {
            "fed_funds": round_or_none(fed_funds, 2),
            "us_2y": round_or_none(us2y, 2),
            "us_10y": round_or_none(us10y, 2),
            "broad_dollar_change_20d": round_or_none(dxy_change_20d, 3),
        },
        "turkey": {
            "policy_rate": round_or_none(policy_latest, 2),
            "official_reserve_assets": round_or_none(official_reserves_latest, 1),
            "fx_reserves": round_or_none(fx_reserves_latest, 1),
            "official_reserve_assets_change_4w": round_or_none(official_reserves_change, 3),
            "fx_reserves_change_4w": round_or_none(fx_reserves_change, 3),
        },
        "scores": {
            "global_pressure": round_or_none(global_pressure, 1),
            "domestic_pressure": round_or_none(domestic_pressure, 1),
        },
    }


def build_news_section(
    headlines: list[FeedEntry],
    chatter: list[FeedEntry],
    *,
    headline_available: bool = True,
    chatter_available: bool = True,
) -> dict:
    now = datetime.now(UTC).replace(tzinfo=None)
    lookback = now - timedelta(days=14)
    headline_recent = [entry for entry in headlines if entry.published_at >= lookback]
    chatter_recent = [entry for entry in chatter if entry.published_at >= lookback]
    headline_count = len(headline_recent) if headline_available else None
    chatter_count = len(chatter_recent) if chatter_available else None
    news_pressure = mean_available(
        [
            score_scale(headline_count, 5, 40),
            score_scale(chatter_count, 3, 25),
        ]
    )
    return {
        "headline_count_14d": headline_count,
        "chatter_count_14d": chatter_count,
        "headline_feed_available": headline_available,
        "chatter_feed_available": chatter_available,
        "score": round(news_pressure, 1) if news_pressure is not None else None,
        "recent_headlines": [
            {
                "title": entry.title,
                "link": entry.link,
                "published_at": format_date(entry.published_at),
            }
            for entry in headline_recent[:6]
        ],
    }


def build_market_trend_chart(
    usd_try: list[SeriesPoint],
    peer_pairs: dict[str, list[SeriesPoint]],
    lookback: int = 20,
) -> dict:
    usd_path = normalized_change_points(usd_try, lookback)
    peer_path = build_peer_basket_path(peer_pairs, lookback)
    return {
        "title": "USD/TRY vs peer basket",
        "subtitle": "Last 20 sessions, normalized to 0% at the start of the window.",
        "unit": "Percent change from start",
        "series": [
            {"label": "USD/TRY", "points": usd_path},
            {"label": "Peer basket", "points": peer_path},
        ],
    }


def build_score_history_chart(history: list[dict], lookback: int = 30) -> dict:
    comparable = [
        entry
        for entry in history
        if entry.get("model_version") == MODEL_VERSION and entry.get("forecast_id")
    ]
    points = [
        {
            "date": entry.get("as_of", "")[:10],
            "value": round(entry.get("primary_score", 0.0), 1),
            "stance": entry.get("stance", "n/a"),
        }
        for entry in comparable[-lookback:]
        if entry.get("as_of") and entry.get("primary_score") is not None
    ]
    return {
        "title": "Primary score history",
        "subtitle": f"Publications from model {MODEL_VERSION} only; legacy index history is excluded.",
        "unit": "Experimental probability estimate (%)",
        "series": [
            {
                "label": "Primary score",
                "points": points,
            }
        ],
    }


def build_peer_basket_path(peer_pairs: dict[str, list[SeriesPoint]], lookback: int) -> list[dict]:
    normalized_paths = [
        normalized_change_points(points, lookback)
        for points in peer_pairs.values()
    ]
    usable_paths = [path for path in normalized_paths if len(path) >= 2]
    if not usable_paths:
        return []

    common_length = min(len(path) for path in usable_paths)
    trimmed = [path[-common_length:] for path in usable_paths]
    return [
        {
            "date": trimmed[0][index]["date"],
            "value": round(mean(path[index]["value"] for path in trimmed), 2),
        }
        for index in range(common_length)
    ]


def normalized_change_points(points: list[SeriesPoint], lookback: int) -> list[dict]:
    if len(points) < 2:
        return []
    trimmed = points[-lookback:]
    base_value = trimmed[0].value
    if base_value == 0:
        return []
    return [
        {
            "date": format_date(point.observed_at),
            "value": round(((point.value - base_value) / base_value) * 100.0, 2),
        }
        for point in trimmed
    ]


def build_risk_curve(market: dict, macro: dict, news: dict) -> dict[str, float]:
    market_pressure = market["scores"]["market_pressure"]
    volatility_pressure = market["scores"]["volatility_pressure"]
    global_pressure = macro["scores"]["global_pressure"]
    domestic_pressure = macro["scores"]["domestic_pressure"]
    news_pressure = news["score"]

    weights = {
        "1w": (0.40, 0.35, 0.15, 0.05, 0.05),
        "1m": (0.30, 0.25, 0.20, 0.20, 0.05),
        "3m": (0.20, 0.15, 0.25, 0.30, 0.10),
        "6m": (0.15, 0.10, 0.25, 0.35, 0.15),
        "1y": (0.10, 0.05, 0.25, 0.40, 0.20),
    }
    curve: dict[str, float] = {}
    for horizon, (w_market, w_vol, w_global, w_domestic, w_news) in weights.items():
        available = [
            (market_pressure, w_market),
            (volatility_pressure, w_vol),
            (global_pressure, w_global),
            (domestic_pressure, w_domestic),
            (news_pressure, w_news),
        ]
        available = [(value, weight) for value, weight in available if value is not None]
        if not available:
            continue
        score = sum(value * weight for value, weight in available) / sum(weight for _, weight in available)
        curve[horizon] = round(max(5.0, min(95.0, score)), 1)
    return curve


def build_headline(primary_score: float, market_regime: str, macro_regime: str) -> str:
    return (
        f"{risk_band_label(primary_score)}: {market_regime.lower()} while {macro_regime.lower()}."
    )


def build_summary(primary_score: float, market: dict, macro: dict, news: dict, briefing: dict) -> dict:
    return {
        "deck": briefing["house_call"],
        "primary_message": briefing["house_call"],
        "market_message": (
            f"USD/TRY is {format_change(market['usd_try']['change_20d'])} over 20 sessions, "
            f"with TRY vs peers at {format_change(market['try_gap_20d'])}."
        ),
        "macro_message": build_macro_message(macro),
        "news_message": build_news_message(news),
    }


def build_briefing(
    primary_horizon: str,
    primary_score: float,
    market: dict,
    macro: dict,
    news: dict,
    warnings: list[str],
) -> dict:
    stance = risk_band_label(primary_score).replace(" risk", "").replace(" backdrop", "")
    confidence = infer_confidence(market, macro, warnings)
    caveat = build_caveat_summary(macro, warnings)
    return {
        "stance": stance,
        "probability": round(primary_score, 1),
        "primary_horizon": primary_horizon,
        "confidence": confidence,
        "caveat_severity": caveat["severity"],
        "caveat_message": caveat["message"],
        "house_call": build_house_call(primary_score, market, macro, news, caveat),
    }


def build_why_read(market: dict, macro: dict, news: dict, briefing: dict) -> list[dict]:
    return [
        {
            "label": "Pressure",
            "title": "Spot pressure is still live",
            "detail": (
                f"TRY is {format_change(market['try_gap_20d'])} weaker than the peer basket over 20 sessions, "
                f"while USD/TRY is {format_change(market['usd_try']['change_20d'])} over the same window."
            ),
        },
        {
            "label": "Domestic context",
            "title": domestic_lens_title(macro),
            "detail": domestic_lens_detail(macro),
        },
        {
            "label": "Unclear",
            "title": "External macro is still cloudy",
            "detail": build_unclear_message(macro, news, briefing),
        },
    ]


def build_trigger_cards(market: dict, macro: dict, news: dict) -> list[dict]:
    triggers = [
        {
            "title": "TRY vs peers",
            "detail": (
                f"If TRY keeps lagging the peer basket beyond the current {format_change(market['try_gap_20d'])} gap, "
                "the read should move more bearish."
            ),
            "now": f"Now {format_change(market['try_gap_20d'])} over 20 sessions",
        },
        {
            "title": "Reserve trend",
            "detail": (
                "A fresh multi-week CBRT reserve series is required before reserve momentum can be described as "
                "supportive or deteriorating."
                if macro["turkey"]["official_reserve_assets_change_4w"] is None
                else "If reserve momentum rolls over from its current trend, the domestic cushion gets thinner."
            ),
            "now": (
                "Unavailable in this snapshot"
                if macro["turkey"]["official_reserve_assets_change_4w"] is None
                else f"Now {format_change(macro['turkey']['official_reserve_assets_change_4w'])} over 4 weeks"
            ),
        },
    ]

    if macro["global"]["broad_dollar_change_20d"] is None and macro["global"]["us_2y"] is None:
        triggers.append(
            {
                "title": "Global macro clarity",
                "detail": (
                    "If the missing global-rates and dollar feeds come back showing stronger USD pressure, the current "
                    "neutral external read should move higher."
                ),
                "now": "Now external macro read is neutralized by missing feeds",
            }
        )
    else:
        triggers.append(
            {
                "title": "Volatility regime",
                "detail": (
                    "If global and EM volatility break higher together, shorter-horizon TRY risk should rise quickly."
                ),
                "now": (
                    f"Now VIX/VXEEM are {format_number(market['volatility']['VIX'])}/"
                    f"{format_number(market['volatility']['VXEEM'])}"
                ),
            }
        )

    if count_at_least(news["headline_count_14d"], 6):
        triggers[2] = {
            "title": "Headline intensity",
            "detail": (
                "If policy, inflation, or sanctions headlines start clustering more aggressively, the read should turn "
                "less patient."
            ),
            "now": f"Now {format_count(news['headline_count_14d'])} news items in 14 days",
        }

    return triggers


def build_reasons(market: dict, macro: dict, news: dict) -> list[dict]:
    candidates = [
        (
            market["scores"]["market_pressure"],
            "Price action",
            f"USD/TRY 20-session move is {format_change(market['usd_try']['change_20d'])}.",
        ),
        (
            market["scores"]["volatility_pressure"],
            "Volatility",
            (
                f"VIX is {format_number(market['volatility']['VIX'])}, "
                f"VXEEM is {format_number(market['volatility']['VXEEM'])}."
            ),
        ),
        (
            macro["scores"]["global_pressure"],
            "Global macro",
            build_global_reason(macro),
        ),
        (
            macro["scores"]["domestic_pressure"],
            "Turkey policy and reserves",
            domestic_lens_detail(macro),
        ),
        (
            news["score"],
            "Headline flow",
            f"{format_count(news['headline_count_14d'])} news items and {format_count(news['chatter_count_14d'])} chatter items in 14 days.",
        ),
    ]
    candidates = [item for item in candidates if isinstance(item[0], (int, float))]
    candidates.sort(key=lambda item: item[0], reverse=True)
    return [
        {"title": title, "detail": detail, "score": round(score, 1)}
        for score, title, detail in candidates[:4]
    ]


def build_watchlist(market: dict, macro: dict, news: dict) -> list[str]:
    return [
        (
            "Watch whether USD/TRY keeps outrunning the peer basket; "
            f"current 20-session gap is {format_change(market['try_gap_20d'])}."
        ),
        (
            "Watch for a fresh CBRT reserve series before drawing a trend conclusion."
            if macro["turkey"]["official_reserve_assets_change_4w"] is None
            else "Watch reserve trend updates from CBRT; official reserve assets are "
            f"{format_change(macro['turkey']['official_reserve_assets_change_4w'])} over the latest reserve window."
        ),
        (
            "Watch global volatility proxies; VIX/VXEEM are "
            f"{format_number(market['volatility']['VIX'])}/{format_number(market['volatility']['VXEEM'])}."
        ),
        (
            "Watch headline intensity; the current 14-day count is "
            f"{format_count(news['headline_count_14d'])} news items."
        ),
    ]


def risk_band_label(primary_score: float) -> str:
    if primary_score >= 70:
        return "High depreciation risk"
    if primary_score >= 50:
        return "Elevated depreciation risk"
    if primary_score >= 35:
        return "Balanced but fragile backdrop"
    return "Contained depreciation risk"


def build_house_call(primary_score: float, market: dict, macro: dict, news: dict, caveat: dict) -> str:
    if caveat["severity"] == "high":
        return "TRY is lagging peers, while incomplete domestic context limits the macro interpretation of the experimental price-history signal."
    if count_at_least(news["headline_count_14d"], 6):
        return "TRY is under pressure and the headline backdrop is getting busier; domestic context should be read only where its source panel is fresh."
    if primary_score >= 50:
        return "TRY is under visible market pressure; the domestic evidence is shown separately and does not alter the experimental model output."
    return "TRY market pressure is contained in this snapshot; incomplete domestic evidence prevents a stronger policy or reserve conclusion."


def infer_confidence(market: dict, macro: dict, warnings: list[str]) -> str:
    score = 0
    if market["usd_try"]["latest"] is not None:
        score += 2
    if market["try_gap_20d"] is not None:
        score += 1
    if market["volatility"]["VIX"] is not None and market["volatility"]["VXEEM"] is not None:
        score += 1
    if macro["turkey"]["policy_rate"] is not None:
        score += 1
    if macro["turkey"]["official_reserve_assets"] is not None:
        score += 1
    if any(
        value is not None
        for value in (
            macro["global"]["fed_funds"],
            macro["global"]["us_2y"],
            macro["global"]["broad_dollar_change_20d"],
        )
    ):
        score += 1
    score -= min(len(warnings), 4) * 0.5

    if score >= 5.5:
        return "high"
    if score >= 3.5:
        return "medium"
    return "low"


def build_caveat_summary(macro: dict, warnings: list[str]) -> dict:
    global_missing = (
        macro["global"]["fed_funds"] is None
        and macro["global"]["us_2y"] is None
        and macro["global"]["broad_dollar_change_20d"] is None
    )
    policy_missing = macro["turkey"]["policy_rate"] is None
    reserves_missing = macro["turkey"]["official_reserve_assets_change_4w"] is None
    if global_missing:
        return {
            "severity": "high",
            "message": (
                "Global rates and dollar feeds were incomplete, so the external macro read is less certain than the "
                "market and domestic read."
            ),
        }
    if policy_missing and reserves_missing:
        return {
            "severity": "high",
            "message": (
                "Fresh CBRT policy-rate and multi-week reserve evidence was unavailable. The price-history model can "
                "still run, but its domestic macro context is incomplete."
            ),
        }
    if policy_missing or reserves_missing or warnings:
        return {
            "severity": "medium",
            "message": (
                "One or more contextual public feeds were unavailable. The core price-history model is unaffected, "
                "but the missing context must not be read as neutral evidence."
            ),
        }
    return {
        "severity": "low",
        "message": "Public feeds landed cleanly in this snapshot.",
    }


def build_data_health(source_cache: dict[str, object], warnings: list[str]) -> dict:
    sources_payload = source_cache.get("sources", {})
    sources: list[dict] = []
    if isinstance(sources_payload, dict):
        for key, value in sorted(sources_payload.items()):
            if not isinstance(value, dict):
                continue
            latest_text = value.get("latest_observation")
            latest_date = None
            if isinstance(latest_text, str):
                try:
                    latest_date = datetime.fromisoformat(latest_text.replace("Z", "+00:00"))
                except ValueError:
                    latest_date = None
            sources.append(
                {
                    "key": key,
                    "id": key,
                    "label": value.get("label", key),
                    "status": value.get("status", "unknown"),
                    "fetched_at": value.get("fetched_at"),
                    "latest_observation": latest_text,
                    "observed_at": latest_text,
                    "age_days": round(age_days(latest_date), 1) if latest_date is not None else None,
                    "item_count": value.get("item_count", 0),
                    "checksum_sha256": value.get("checksum_sha256"),
                    "used_cache": bool(value.get("used_cache", False)),
                    "stale_after_days": value.get("stale_after_days"),
                    "last_error": value.get("last_error"),
                }
            )
    for source in sources:
        if source.get("observed_at") is None:
            source.pop("observed_at", None)
    status_by_key = {source["key"]: source["status"] for source in sources}
    usable_statuses = {"fresh", "cached_fallback"}
    forecast_required = ("ecb_eurtry", "ecb_eurusd")
    forecast_ready = all(status_by_key.get(key) in usable_statuses for key in forecast_required)
    unavailable = [source["key"] for source in sources if source["status"] not in usable_statuses]
    overall = "healthy" if not unavailable and not warnings else "degraded"
    if not forecast_ready:
        overall = "blocked"
    fresh_count = sum(source["status"] == "fresh" for source in sources)
    stale_count = sum(source["status"] == "cached_fallback" for source in sources)
    unavailable_count = sum(source["status"] not in usable_statuses for source in sources)
    return {
        "status": overall,
        "overall_status": overall,
        "forecast_ready": forecast_ready,
        "forecast_required_sources": list(forecast_required),
        "available_source_count": sum(source["status"] in usable_statuses for source in sources),
        "total_source_count": len(sources),
        "fresh_count": fresh_count,
        "stale_count": stale_count,
        "unavailable_count": unavailable_count,
        "coverage_ratio": round(
            sum(source["status"] in usable_statuses for source in sources) / len(sources), 3
        ) if sources else 0.0,
        "unavailable_or_stale_sources": unavailable,
        "missing_data_policy": (
            "Missing, stale, empty or semantically invalid inputs are unavailable; they are never converted to neutral evidence."
        ),
        "cache_policy": (
            "Only semantically valid, fresh responses replace last-good payloads. Cached data must independently pass "
            "the same validation and observation-age gate."
        ),
        "sources": sources,
    }


def build_unclear_message(macro: dict, news: dict, briefing: dict) -> str:
    if briefing["caveat_severity"] == "high":
        return briefing["caveat_message"]
    if news["headline_count_14d"] == 0 and news["chatter_count_14d"] == 0:
        return "Headline flow is quiet, so the model leans more on market and macro signals than on narrative stress."
    if macro["global"]["broad_dollar_change_20d"] is None:
        return "The external macro read is still softer than the market read because key global-rate feeds were incomplete."
    return briefing["caveat_message"]


def load_source_cache() -> dict[str, object]:
    if not SOURCE_CACHE_PATH.exists():
        return {"schema_version": "2.0", "sources": {}}
    try:
        payload = json.loads(SOURCE_CACHE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"schema_version": "2.0", "sources": {}}
    if isinstance(payload, dict) and isinstance(payload.get("sources"), dict):
        payload["schema_version"] = "2.0"
        return payload

    # One-time migration from the v1 payload-only cache. The old data remains
    # available as a fallback but must pass the v2 semantic and freshness gates.
    migrated: dict[str, object] = {"schema_version": "2.0", "sources": {}}
    generated_at = None
    try:
        generated_at = json.loads(LATEST_PATH.read_text(encoding="utf-8")).get("generated_at")
    except (OSError, json.JSONDecodeError, AttributeError):
        pass
    for key, cached_payload in payload.items() if isinstance(payload, dict) else []:
        migrated["sources"][key] = {
            "label": key,
            "payload": cached_payload,
            "fetched_at": generated_at,
            "last_attempt_at": None,
            "latest_observation": _latest_serialized_observation(cached_payload),
            "item_count": _serialized_item_count(cached_payload),
            "checksum_sha256": checksum(cached_payload),
            "status": "legacy_unverified",
            "used_cache": False,
            "stale_after_days": SOURCE_RULES.get(key, (None, 30))[1],
            "last_error": None,
        }
    return migrated


def _latest_serialized_observation(payload: object) -> str | None:
    candidates: list[str] = []
    if isinstance(payload, list):
        for item in payload:
            if not isinstance(item, dict):
                continue
            value = item.get("observed_at") or item.get("published_at")
            if isinstance(value, str):
                candidates.append(value)
    elif isinstance(payload, dict):
        for value in payload.values():
            nested = _latest_serialized_observation(value)
            if nested is not None:
                candidates.append(nested)
    if not candidates:
        return None
    latest = max(candidates)
    try:
        return iso_utc(datetime.fromisoformat(latest.replace("Z", "+00:00")))
    except ValueError:
        return latest


def _serialized_item_count(payload: object) -> int:
    if isinstance(payload, list):
        return len(payload)
    if isinstance(payload, dict):
        return sum(_serialized_item_count(value) for value in payload.values())
    return 0


def serialize_series(points: list[SeriesPoint], keep: int = 800) -> list[dict]:
    trimmed = points[-keep:]
    return [
        {
            "observed_at": point.observed_at.isoformat(),
            "value": point.value,
        }
        for point in trimmed
    ]


def serialize_series_long(points: list[SeriesPoint]) -> list[dict]:
    """Retain the long spot/peer history required by the empirical model."""
    return serialize_series(points, keep=5000)


def deserialize_series(payload: object) -> list[SeriesPoint]:
    if not isinstance(payload, list):
        return []
    return [
        SeriesPoint(
            observed_at=datetime.fromisoformat(str(item["observed_at"])),
            value=float(item["value"]),
        )
        for item in payload
        if isinstance(item, dict) and "observed_at" in item and "value" in item
    ]


def serialize_series_map(series_map: dict[str, list[SeriesPoint]]) -> dict[str, list[dict]]:
    return {
        key: serialize_series(points)
        for key, points in series_map.items()
    }


def deserialize_series_map(payload: object) -> dict[str, list[SeriesPoint]]:
    if not isinstance(payload, dict):
        return {}
    return {
        str(key): deserialize_series(value)
        for key, value in payload.items()
    }


def serialize_feed(entries: list[FeedEntry]) -> list[dict]:
    return [
        {
            "title": entry.title,
            "link": entry.link,
            "published_at": entry.published_at.isoformat(),
        }
        for entry in entries
    ]


def deserialize_feed(payload: object) -> list[FeedEntry]:
    if not isinstance(payload, list):
        return []
    return [
        FeedEntry(
            title=str(item["title"]),
            link=str(item["link"]) if item.get("link") is not None else None,
            published_at=datetime.fromisoformat(str(item["published_at"])),
        )
        for item in payload
        if isinstance(item, dict) and "title" in item and "published_at" in item
    ]


def update_history(snapshot: dict) -> list[dict]:
    if HISTORY_PATH.exists():
        history = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
    else:
        history = []

    entry = snapshot["history_entry"]
    entry_day = entry["as_of"][:10]
    updated = False
    for index, existing in enumerate(history):
        if existing.get("as_of", "")[:10] == entry_day:
            history[index] = entry
            updated = True
            break
    if not updated:
        history.append(entry)
    history.sort(key=lambda item: item.get("as_of", ""))
    # Keep enough daily publications for more than one year. The dedicated
    # forecast ledger is append-only and is never trimmed.
    return history[-550:]


def fetch_ecb_series(series_code: str, *, last_n: int = 5000) -> list[SeriesPoint]:
    url = (
        "https://data-api.ecb.europa.eu/service/data/EXR/"
        f"{series_code}?lastNObservations={last_n}&format=csvdata"
    )
    csv_text = fetch_text(url, accept="text/csv, */*")
    points: list[SeriesPoint] = []
    for row in DictReader(StringIO(csv_text)):
        date_value = parse_date(row.get("TIME_PERIOD"))
        close_value = parse_float(row.get("OBS_VALUE"))
        if date_value is None or close_value is None:
            continue
        points.append(SeriesPoint(date_value, close_value))
    return sorted(points, key=lambda item: item.observed_at)


def fetch_fred_series(series_code: str) -> list[SeriesPoint]:
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={quote(series_code)}"
    csv_text = fetch_text(url, accept="text/csv, */*")
    points: list[SeriesPoint] = []
    for row in DictReader(StringIO(csv_text)):
        # FRED changed this export header from DATE to observation_date. Accept
        # both so an upstream naming change cannot silently neutralize the lens.
        date_value = parse_date(row.get("observation_date") or row.get("DATE"))
        close_value = parse_float(row.get(series_code))
        if date_value is None or close_value is None:
            continue
        points.append(SeriesPoint(date_value, close_value))
    return sorted(points, key=lambda item: item.observed_at)


def fetch_cboe_series(symbol: str) -> list[SeriesPoint]:
    url = f"https://cdn.cboe.com/api/global/us_indices/daily_prices/{symbol}_History.csv"
    csv_text = fetch_text(url, accept="text/csv, */*")
    points: list[SeriesPoint] = []
    for row in DictReader(StringIO(csv_text)):
        date_value = parse_date(row.get("DATE"))
        close_value = parse_float(
            row.get("CLOSE")
            or row.get(symbol)
            or row.get(symbol.upper())
            or row.get(symbol.lower())
        )
        if date_value is None or close_value is None:
            continue
        points.append(SeriesPoint(date_value, close_value))
    return sorted(points, key=lambda item: item.observed_at)


def fetch_cbrt_policy_rate() -> list[SeriesPoint]:
    html_text = fetch_text(CBRT_POLICY_RATE_URL)
    parser = VisibleTextParser()
    parser.feed(html_text)
    matches = re.findall(
        r"(?P<date>\d{2}\.\d{2}\.\d{4})\s*-\s*(?P<rate>\d{1,2}(?:[.,]\d{1,2})?)",
        parser.text(),
    )
    points: dict[datetime, SeriesPoint] = {}
    for date_text, rate_text in matches:
        date_value = parse_date(date_text)
        rate_value = parse_float(rate_text)
        if date_value is None or rate_value is None:
            continue
        points[date_value] = SeriesPoint(date_value, rate_value)
    return sorted(points.values(), key=lambda item: item.observed_at)


def fetch_cbrt_reserves() -> dict[str, list[SeriesPoint]]:
    html_text = fetch_text(CBRT_RESERVES_URL)
    zip_url = extract_cbrt_irfcl_zip_url(html_text, CBRT_RESERVES_URL)
    zip_bytes = fetch_bytes(zip_url)
    return {
        series_name: parse_cbrt_irfcl_points(zip_bytes, row_label)
        for series_name, row_label in CBRT_IRFCL_ROW_LABELS.items()
    }


def fetch_rss_entries(url: str) -> list[FeedEntry]:
    xml_text = fetch_text(url, accept="application/rss+xml, application/xml, text/xml, */*")
    root = ElementTree.fromstring(xml_text)
    channel = root.find("channel")
    items = channel.findall("item") if channel is not None else root.findall(".//item")
    entries: list[FeedEntry] = []
    for item in items[:25]:
        title = clean_xml_text(item.findtext("title")) or "Untitled entry"
        link = clean_xml_text(item.findtext("link"))
        published = parse_feed_datetime(clean_xml_text(item.findtext("pubDate")))
        entries.append(FeedEntry(title=title, link=link, published_at=published))
    return entries


def derive_usd_cross(series_in_quote: list[SeriesPoint], eur_usd: list[SeriesPoint]) -> list[SeriesPoint]:
    eur_usd_map = {point.observed_at.date(): point.value for point in eur_usd}
    derived: list[SeriesPoint] = []
    for point in series_in_quote:
        eur_usd_value = eur_usd_map.get(point.observed_at.date())
        if eur_usd_value is None or eur_usd_value == 0:
            continue
        derived.append(SeriesPoint(point.observed_at, point.value / eur_usd_value))
    return derived


def fetch_text(url: str, *, accept: str | None = None) -> str:
    last_error: Exception | None = None
    user_agents = [
        (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0 Safari/537.36"
        ),
        "Mozilla/5.0",
    ]
    for attempt in range(2):
        request = Request(
            url,
            headers={
                "User-Agent": user_agents[attempt % len(user_agents)],
                "Connection": "close",
                **({"Accept": accept} if accept else {}),
            },
        )
        try:
            with urlopen(request, timeout=15) as response:
                return response.read().decode("utf-8", errors="replace")
        except (HTTPError, URLError, TimeoutError) as exc:
            last_error = exc
            if attempt < 1:
                sleep(1)
    raise RuntimeError(f"Fetch failed for {url}: {last_error}") from last_error


def fetch_bytes(url: str) -> bytes:
    last_error: Exception | None = None
    user_agents = [
        (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0 Safari/537.36"
        ),
        "Mozilla/5.0",
    ]
    for attempt in range(2):
        request = Request(
            url,
            headers={
                "User-Agent": user_agents[attempt % len(user_agents)],
                "Connection": "close",
            },
        )
        try:
            with urlopen(request, timeout=15) as response:
                return response.read()
        except (HTTPError, URLError, TimeoutError) as exc:
            last_error = exc
            if attempt < 1:
                sleep(1)
    raise RuntimeError(f"Fetch failed for {url}: {last_error}") from last_error


def extract_cbrt_irfcl_zip_url(html_text: str, base_url: str) -> str:
    parser = AnchorCollector()
    parser.feed(html_text)
    for href, anchor_text in parser.anchors:
        if ".zip" not in href.casefold():
            continue
        if CBRT_IRFCL_WEEKLY_ZIP_TEXT in anchor_text.casefold():
            return urljoin(base_url, href)
    for href, _anchor_text in parser.anchors:
        if ".zip" in href.casefold():
            return urljoin(base_url, href)
    raise RuntimeError("Could not find the CBRT reserve ZIP link.")


def parse_cbrt_irfcl_points(zip_bytes: bytes, target_label: str) -> list[SeriesPoint]:
    with zipfile.ZipFile(BytesIO(zip_bytes)) as outer_zip:
        workbook_name = next(
            (
                info.filename
                for info in outer_zip.infolist()
                if info.filename.casefold().endswith(".xlsx")
            ),
            None,
        )
        if workbook_name is None:
            raise RuntimeError("CBRT reserve ZIP did not contain an XLSX workbook.")
        workbook_bytes = outer_zip.read(workbook_name)

    with zipfile.ZipFile(BytesIO(workbook_bytes)) as workbook_zip:
        shared_strings = read_shared_strings(workbook_zip)
        sheet_rows = read_sheet_rows(workbook_zip, shared_strings)

    # The public workbook has moved its weekly header between releases. Detect
    # the row with the largest set of date-like cells instead of depending on a
    # fixed Excel row number.
    header_row = max(
        sheet_rows.values(),
        key=lambda row: len(weekly_columns(row)),
        default={},
    )
    target_row = next(
        (
            row_number
            for row_number, row_values in sheet_rows.items()
            if any(
                normalize_match_key(value) == normalize_match_key(target_label)
                for value in row_values.values()
            )
        ),
        None,
    )
    if target_row is None:
        return []

    dated_columns = weekly_columns(header_row)
    values = sheet_rows[target_row]
    points: list[SeriesPoint] = []
    for column, observed_at in dated_columns:
        value = parse_float(values.get(column))
        if value is None:
            continue
        points.append(SeriesPoint(observed_at, value))
    return sorted(points, key=lambda item: item.observed_at)


def read_shared_strings(workbook_zip: zipfile.ZipFile) -> list[str]:
    shared_strings_xml = workbook_zip.read("xl/sharedStrings.xml")
    root = ElementTree.fromstring(shared_strings_xml)
    namespace = {"a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    values: list[str] = []
    for item in root.findall("a:si", namespace):
        values.append("".join(node.text or "" for node in item.iterfind(".//a:t", namespace)))
    return values


def read_sheet_rows(workbook_zip: zipfile.ZipFile, shared_strings: list[str]) -> dict[int, dict[str, str]]:
    sheet_xml = workbook_zip.read("xl/worksheets/sheet1.xml")
    root = ElementTree.fromstring(sheet_xml)
    namespace = {"a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    rows: dict[int, dict[str, str]] = {}
    for row in root.findall(".//a:sheetData/a:row", namespace):
        row_number = int(row.attrib["r"])
        values: dict[str, str] = {}
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
            values[column] = value
        rows[row_number] = values
    return rows


def weekly_columns(header_row: dict[str, str]) -> list[tuple[str, datetime]]:
    columns: list[tuple[str, datetime]] = []
    for column, value in header_row.items():
        date_value = parse_irfcl_header_date(value)
        if date_value is not None:
            columns.append((column, date_value))
    columns.sort(key=lambda item: item[1])
    return columns


def parse_irfcl_header_date(value: str | None) -> datetime | None:
    direct = parse_date(value)
    candidate = direct
    if candidate is None:
        numeric = parse_float(value)
        if numeric is None:
            return None
        candidate = datetime(1899, 12, 30) + timedelta(days=numeric)
    earliest = datetime(1990, 1, 1)
    latest = datetime.now(UTC).replace(tzinfo=None) + timedelta(days=31)
    return candidate if earliest <= candidate <= latest else None


def latest_point(points: list[SeriesPoint]) -> SeriesPoint | None:
    return points[-1] if points else None


def latest_value(points: list[SeriesPoint]) -> float | None:
    point = latest_point(points)
    return point.value if point is not None else None


def percent_change(points: list[SeriesPoint], steps_back: int) -> float | None:
    if len(points) <= steps_back:
        return None
    current = points[-1].value
    previous = points[-1 - steps_back].value
    if previous == 0:
        return None
    return ((current - previous) / previous) * 100.0


def percent_change_window(points: list[SeriesPoint], preferred_steps: int) -> float | None:
    if len(points) < 2:
        return None
    steps_back = min(preferred_steps, len(points) - 1)
    return percent_change(points, steps_back)


def score_scale(value: float | None, low: float, high: float) -> float | None:
    if value is None:
        return None
    if high == low:
        return 50.0
    scaled = ((value - low) / (high - low)) * 100.0
    return max(0.0, min(100.0, scaled))


def mean_available(values: list[float | None]) -> float | None:
    available = [value for value in values if value is not None]
    return mean(available) if available else None


def negative_or_zero(value: float | None) -> float | None:
    if value is None:
        return None
    return max(0.0, -value)


def parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d-%m-%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(normalized, fmt)
        except ValueError:
            continue
    if len(normalized) == 4 and normalized.isdigit():
        return datetime(int(normalized), 1, 1)
    return None


def parse_float(value: object) -> float | None:
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


def clean_xml_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = unescape(value).strip()
    return cleaned or None


def parse_feed_datetime(value: str | None) -> datetime:
    if not value:
        return datetime.now(UTC).replace(tzinfo=None)
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return datetime.now(UTC).replace(tzinfo=None)
    if parsed.tzinfo is None:
        return parsed
    return parsed.astimezone(UTC).replace(tzinfo=None)


def normalize_match_key(value: str) -> str:
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


def safe_subtract(left: float | None, right: float | None) -> float | None:
    if left is None or right is None:
        return None
    return left - right


def round_or_none(value: float | None, digits: int) -> float | None:
    return round(value, digits) if value is not None else None


def format_date(value: datetime | None) -> str | None:
    return value.strftime("%Y-%m-%d") if value is not None else None


def format_change(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:+.2f}%"


def format_number(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.2f}"


def format_count(value: int | None) -> str:
    return "unavailable" if value is None else str(value)


def count_at_least(value: int | None, threshold: int) -> bool:
    return value is not None and value >= threshold


def build_news_message(news: dict) -> str:
    headline_count = news.get("headline_count_14d")
    chatter_count = news.get("chatter_count_14d")
    parts = [
        f"Google News: {format_count(headline_count)} items",
        f"social chatter: {format_count(chatter_count)} items",
    ]
    unavailable = [
        label
        for label, available in (
            ("Google News", news.get("headline_feed_available")),
            ("social chatter", news.get("chatter_feed_available")),
        )
        if available is False
    ]
    suffix = f"; unavailable feeds: {', '.join(unavailable)}" if unavailable else ""
    return f"{', '.join(parts)} over the last 14 days{suffix}."


def build_macro_message(macro: dict) -> str:
    broad_dollar = macro["global"]["broad_dollar_change_20d"]
    reserve_change = macro["turkey"]["official_reserve_assets_change_4w"]
    if broad_dollar is None:
        return (
            "Global-rates feeds were incomplete in this snapshot. They were marked unavailable and did not alter "
            "the price-history model."
        )
    if reserve_change is None:
        return (
            f"Broad dollar change is {format_change(broad_dollar)}; a valid multi-week CBRT reserve trend was "
            "unavailable in this snapshot."
        )
    return (
        f"Broad dollar change is {format_change(broad_dollar)}, while official reserves are "
        f"{format_change(reserve_change)} over the latest reserve window."
    )


def domestic_lens_title(macro: dict) -> str:
    policy = macro["turkey"]["policy_rate"]
    reserve_change = macro["turkey"]["official_reserve_assets_change_4w"]
    if policy is None and reserve_change is None:
        return "Domestic lens is incomplete"
    if policy is None:
        return "Policy-rate observation is unavailable"
    if reserve_change is None:
        return "Reserve trend is unavailable"
    return "Domestic policy and reserve context"


def domestic_lens_detail(macro: dict) -> str:
    policy = macro["turkey"]["policy_rate"]
    reserve_change = macro["turkey"]["official_reserve_assets_change_4w"]
    if policy is None and reserve_change is None:
        return "Fresh policy-rate and multi-week reserve observations were unavailable; no domestic signal was inferred."
    if policy is None:
        return f"Policy rate is unavailable; official reserves changed {format_change(reserve_change)} over the latest valid window."
    if reserve_change is None:
        return f"Policy rate is {format_number(policy)}%; a valid multi-week reserve trend was unavailable."
    return (
        f"Policy rate is {format_number(policy)}%, and official reserves changed "
        f"{format_change(reserve_change)} over the latest valid window."
    )


def build_global_reason(macro: dict) -> str:
    broad_dollar = macro["global"]["broad_dollar_change_20d"]
    us_2y = macro["global"]["us_2y"]
    if broad_dollar is None and us_2y is None:
        return "Global-rates feeds were incomplete, so this snapshot kept the global backdrop neutral."
    return (
        f"Broad dollar is {format_change(broad_dollar)} over 20 sessions, "
        f"with US 2Y at {format_number(us_2y)}%."
    )


if __name__ == "__main__":
    main()
