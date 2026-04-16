from pathlib import Path

from sqlalchemy import inspect, select, text
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import Base, engine
from app.models.entities import MacroSeries, PriceSeries, Source

DEFAULT_SOURCES = [
    {
        "name": "CBRT Policy Rates",
        "slug": "cbrt-policy-rates",
        "category": "official_macro",
        "trust_tier": 1,
        "collection_method": "html",
        "endpoint": "https://www.tcmb.gov.tr/wps/wcm/connect/EN/TCMB%2BEN/Main%2BMenu/Core%2BFunctions/Monetary%2BPolicy/Central%2BBank%2BInterest%2BRates/1%2BWeek%2BRepo",
        "freshness_expectation": "policy meeting dates",
        "parser_adapter": "cbrt_policy_rate",
    },
    {
        "name": "CBRT Reserve Liquidity",
        "slug": "cbrt-reserves",
        "category": "official_macro",
        "trust_tier": 1,
        "collection_method": "html+zip",
        "endpoint": "https://www.tcmb.gov.tr/wps/wcm/connect/EN/TCMB%2BEN/Main%2BMenu/Statistics/Balance%2Bof%2BPayments%2Band%2BRelated%2BStatistics/International%2BReserves%2Band%2BForeign%2BCurrency%2BLiquidity/",
        "freshness_expectation": "weekly and monthly",
        "parser_adapter": "cbrt_irfcl",
    },
    {
        "name": "CBRT EVDS (Optional Advanced)",
        "slug": "cbrt-evds",
        "category": "official_macro",
        "trust_tier": 1,
        "collection_method": "api",
        "endpoint": "https://evds2.tcmb.gov.tr/",
        "requires_credentials": True,
        "freshness_expectation": "daily to monthly",
        "parser_adapter": "evds",
    },
    {
        "name": "FRED",
        "slug": "fred",
        "category": "official_macro",
        "trust_tier": 1,
        "collection_method": "csv",
        "endpoint": "https://fred.stlouisfed.org/graph/",
        "freshness_expectation": "daily to monthly",
        "parser_adapter": "fred",
    },
    {
        "name": "IMF Data",
        "slug": "imf-data",
        "category": "official_macro",
        "trust_tier": 1,
        "collection_method": "api",
        "endpoint": "https://www.imf.org/external/datamapper/api/",
        "freshness_expectation": "monthly to quarterly",
        "parser_adapter": "imf",
    },
    {
        "name": "ECB EXR",
        "slug": "ecb-exr",
        "category": "market_prices",
        "trust_tier": 1,
        "collection_method": "api",
        "endpoint": "https://data-api.ecb.europa.eu/service/data/EXR/",
        "freshness_expectation": "daily",
        "parser_adapter": "ecb_exr",
    },
    {
        "name": "Cboe Volatility Indices",
        "slug": "cboe-vix",
        "category": "market_prices",
        "trust_tier": 1,
        "collection_method": "csv",
        "endpoint": "https://cdn.cboe.com/api/global/us_indices/daily_prices/",
        "freshness_expectation": "daily",
        "parser_adapter": "cboe_vix_csv",
    },
    {
        "name": "Financial News / RSS",
        "slug": "financial-news-rss",
        "category": "news",
        "trust_tier": 2,
        "collection_method": "rss",
        "endpoint": "https://news.google.com/rss/search?q=(Turkish+lira+OR+CBRT+OR+Turkey+inflation)&hl=en-US&gl=US&ceid=US:en",
        "freshness_expectation": "intraday",
        "parser_adapter": "rss",
    },
    {
        "name": "Market Chatter",
        "slug": "market-chatter",
        "category": "social",
        "trust_tier": 3,
        "collection_method": "social",
        "endpoint": "https://www.reddit.com/search.rss?q=(Turkish%20lira%20OR%20USDTRY%20OR%20CBRT)&sort=new",
        "freshness_expectation": "intraday",
        "parser_adapter": "chatter",
    },
]

DEFAULT_PRICE_SERIES = [
    {
        "source_slug": "ecb-exr",
        "symbol": "D.TRY.EUR.SP00.A",
        "name": "EUR/TRY ECB Reference Rate",
        "base_currency": "EUR",
        "quote_currency": "TRY",
        "frequency": "daily",
    },
    {
        "source_slug": "ecb-exr",
        "symbol": "D.USD.EUR.SP00.A",
        "name": "EUR/USD ECB Reference Rate",
        "base_currency": "EUR",
        "quote_currency": "USD",
        "frequency": "daily",
    },
    {
        "source_slug": "ecb-exr",
        "symbol": "D.ZAR.EUR.SP00.A",
        "name": "EUR/ZAR ECB Reference Rate",
        "base_currency": "EUR",
        "quote_currency": "ZAR",
        "frequency": "daily",
    },
    {
        "source_slug": "ecb-exr",
        "symbol": "D.BRL.EUR.SP00.A",
        "name": "EUR/BRL ECB Reference Rate",
        "base_currency": "EUR",
        "quote_currency": "BRL",
        "frequency": "daily",
    },
    {
        "source_slug": "ecb-exr",
        "symbol": "D.HUF.EUR.SP00.A",
        "name": "EUR/HUF ECB Reference Rate",
        "base_currency": "EUR",
        "quote_currency": "HUF",
        "frequency": "daily",
    },
    {
        "source_slug": "ecb-exr",
        "symbol": "D.PLN.EUR.SP00.A",
        "name": "EUR/PLN ECB Reference Rate",
        "base_currency": "EUR",
        "quote_currency": "PLN",
        "frequency": "daily",
    },
    {
        "source_slug": "cboe-vix",
        "symbol": "VIX",
        "name": "Cboe VIX Index",
        "base_currency": None,
        "quote_currency": None,
        "frequency": "daily",
    },
    {
        "source_slug": "cboe-vix",
        "symbol": "VIX9D",
        "name": "Cboe VIX9D Index",
        "base_currency": None,
        "quote_currency": None,
        "frequency": "daily",
    },
    {
        "source_slug": "cboe-vix",
        "symbol": "VVIX",
        "name": "Cboe VVIX Index",
        "base_currency": None,
        "quote_currency": None,
        "frequency": "daily",
    },
    {
        "source_slug": "cboe-vix",
        "symbol": "VXEEM",
        "name": "Cboe VXEEM Index",
        "base_currency": None,
        "quote_currency": None,
        "frequency": "daily",
    },
    {
        "source_slug": "cboe-vix",
        "symbol": "OVX",
        "name": "Cboe OVX Oil Volatility Index",
        "base_currency": None,
        "quote_currency": None,
        "frequency": "daily",
    },
    {
        "source_slug": "cboe-vix",
        "symbol": "GVZ",
        "name": "Cboe GVZ Gold Volatility Index",
        "base_currency": None,
        "quote_currency": None,
        "frequency": "daily",
    },
]

DEFAULT_MACRO_SERIES = [
    {
        "source_slug": "fred",
        "code": "FEDFUNDS",
        "name": "Effective Federal Funds Rate",
        "category": "global_rates",
        "unit": "percent",
        "frequency": "monthly",
    },
    {
        "source_slug": "fred",
        "code": "DGS10",
        "name": "US 10-Year Treasury Yield",
        "category": "global_rates",
        "unit": "percent",
        "frequency": "daily",
    },
    {
        "source_slug": "fred",
        "code": "DGS2",
        "name": "US 2-Year Treasury Yield",
        "category": "global_rates",
        "unit": "percent",
        "frequency": "daily",
    },
    {
        "source_slug": "fred",
        "code": "DTWEXBGS",
        "name": "Broad Trade-Weighted US Dollar Index",
        "category": "global_dollar",
        "unit": "index",
        "frequency": "daily",
    },
    {
        "source_slug": "imf-data",
        "code": "PCPIPCH/TUR",
        "name": "Turkey CPI Inflation",
        "category": "turkey_inflation",
        "unit": "percent",
        "frequency": "annual",
    },
    {
        "source_slug": "imf-data",
        "code": "BCA_NGDPD/TUR",
        "name": "Turkey Current Account Balance",
        "category": "external_balance",
        "unit": "percent_of_gdp",
        "frequency": "annual",
    },
    {
        "source_slug": "imf-data",
        "code": "NGDP_RPCH/TUR",
        "name": "Turkey Real GDP Growth",
        "category": "growth",
        "unit": "percent",
        "frequency": "annual",
    },
    {
        "source_slug": "cbrt-policy-rates",
        "code": "CBRT_POLICY_RATE_1W_REPO",
        "name": "CBRT One-Week Repo Policy Rate",
        "category": "domestic_rates",
        "unit": "percent",
        "frequency": "daily",
    },
    {
        "source_slug": "cbrt-reserves",
        "code": "CBRT_IRFCL_OFFICIAL_RESERVE_ASSETS",
        "name": "CBRT Official Reserve Assets",
        "category": "reserves",
        "unit": "usd_million",
        "frequency": "weekly",
    },
    {
        "source_slug": "cbrt-reserves",
        "code": "CBRT_IRFCL_FX_RESERVES",
        "name": "CBRT Foreign Currency Reserves",
        "category": "reserves",
        "unit": "usd_million",
        "frequency": "weekly",
    },
]


def ensure_storage(settings: Settings) -> None:
    for directory in settings.storage_dirs:
        Path(directory).mkdir(parents=True, exist_ok=True)


def init_database() -> None:
    Base.metadata.create_all(bind=engine)


def migrate_database() -> None:
    inspector = inspect(engine)
    assessment_cycle_columns = {
        column["name"] for column in inspector.get_columns("assessment_cycles")
    }
    if "parent_cycle_id" not in assessment_cycle_columns:
        with engine.begin() as connection:
            connection.execute(
                text("ALTER TABLE assessment_cycles ADD COLUMN parent_cycle_id INTEGER")
            )


def seed_sources(session: Session) -> None:
    existing = {
        source.slug: source
        for source in session.scalars(select(Source)).all()
    }
    additions = []
    for source_definition in DEFAULT_SOURCES:
        current = existing.get(source_definition["slug"])
        if current is None:
            additions.append(Source(**source_definition))
            continue
        current.name = source_definition["name"]
        current.category = source_definition["category"]
        current.trust_tier = source_definition["trust_tier"]
        current.collection_method = source_definition["collection_method"]
        current.endpoint = source_definition["endpoint"]
        current.requires_credentials = source_definition.get("requires_credentials", False)
        current.freshness_expectation = source_definition["freshness_expectation"]
        current.parser_adapter = source_definition["parser_adapter"]
    if additions:
        session.add_all(additions)
    session.commit()


def seed_macro_series(session: Session) -> None:
    sources = {
        source.slug: source.id
        for source in session.scalars(select(Source)).all()
    }
    existing = {
        series.code: series
        for series in session.scalars(select(MacroSeries)).all()
    }
    additions: list[MacroSeries] = []
    for series_definition in DEFAULT_MACRO_SERIES:
        source_id = sources.get(series_definition["source_slug"])
        if source_id is None:
            continue
        current = existing.get(series_definition["code"])
        if current is None:
            additions.append(
                MacroSeries(
                    code=series_definition["code"],
                    name=series_definition["name"],
                    category=series_definition["category"],
                    unit=series_definition["unit"],
                    frequency=series_definition["frequency"],
                    source_id=source_id,
                )
            )
            continue
        current.name = series_definition["name"]
        current.category = series_definition["category"]
        current.unit = series_definition["unit"]
        current.frequency = series_definition["frequency"]
        current.source_id = source_id

    obsolete_codes = {"DEXTUUS"}
    for code in obsolete_codes:
        stale = existing.get(code)
        if stale is not None:
            session.delete(stale)

    if additions:
        session.add_all(additions)
    session.commit()


def seed_price_series(session: Session) -> None:
    sources = {
        source.slug: source.id
        for source in session.scalars(select(Source)).all()
    }
    existing = {
        series.symbol: series
        for series in session.scalars(select(PriceSeries)).all()
    }
    additions: list[PriceSeries] = []
    for series_definition in DEFAULT_PRICE_SERIES:
        source_id = sources.get(series_definition["source_slug"])
        if source_id is None:
            continue
        current = existing.get(series_definition["symbol"])
        if current is None:
            additions.append(
                PriceSeries(
                    symbol=series_definition["symbol"],
                    name=series_definition["name"],
                    quote_currency=series_definition["quote_currency"],
                    base_currency=series_definition["base_currency"],
                    frequency=series_definition["frequency"],
                    source_id=source_id,
                )
            )
            continue
        current.name = series_definition["name"]
        current.quote_currency = series_definition["quote_currency"]
        current.base_currency = series_definition["base_currency"]
        current.frequency = series_definition["frequency"]
        current.source_id = source_id
    if additions:
        session.add_all(additions)
    session.commit()


def bootstrap_application(settings: Settings, session: Session) -> None:
    ensure_storage(settings)
    init_database()
    migrate_database()
    session.expire_all()
    seed_sources(session)
    seed_macro_series(session)
    seed_price_series(session)
