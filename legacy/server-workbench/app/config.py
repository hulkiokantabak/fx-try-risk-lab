from functools import lru_cache
from pathlib import Path
from secrets import token_urlsafe

from pydantic import Field, PrivateAttr
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    app_name: str = Field(default="FX TRY Risk Lab", alias="FX_APP_NAME")
    environment: str = Field(default="development", alias="FX_ENVIRONMENT")
    database_url: str = Field(default="sqlite:///data/app.db", alias="FX_DATABASE_URL")
    host: str = Field(default="127.0.0.1", alias="FX_HOST")
    port: int = Field(default=8000, alias="FX_PORT")
    reload_enabled: bool = Field(default=False, alias="FX_RELOAD")
    forwarded_allow_ips: str = Field(default="127.0.0.1", alias="FX_FORWARDED_ALLOW_IPS")
    data_dir: Path = Field(default=BASE_DIR / "data", alias="FX_DATA_DIR")
    reports_dir: Path = Field(default=BASE_DIR / "data" / "reports", alias="FX_REPORTS_DIR")
    exports_dir: Path = Field(default=BASE_DIR / "data" / "exports", alias="FX_EXPORTS_DIR")
    evidence_dir: Path = Field(
        default=BASE_DIR / "data" / "evidence_packs",
        alias="FX_EVIDENCE_DIR",
    )
    raw_data_dir: Path = Field(default=BASE_DIR / "data" / "raw", alias="FX_RAW_DATA_DIR")
    normalized_data_dir: Path = Field(
        default=BASE_DIR / "data" / "normalized",
        alias="FX_NORMALIZED_DATA_DIR",
    )
    log_dir: Path = Field(default=BASE_DIR / "data" / "logs", alias="FX_LOG_DIR")
    allowed_hosts_raw: str = Field(
        default="127.0.0.1,localhost,testserver",
        alias="FX_ALLOWED_HOSTS",
    )
    session_secret_raw: str | None = Field(
        default=None,
        alias="FX_SESSION_SECRET",
    )
    secure_cookies_raw: bool | None = Field(default=None, alias="FX_SECURE_COOKIES")
    fred_api_key: str | None = Field(default=None, alias="FX_FRED_API_KEY")
    evds_api_key: str | None = Field(default=None, alias="FX_EVDS_API_KEY")
    source_fetch_timeout_seconds: int = Field(
        default=15,
        alias="FX_SOURCE_FETCH_TIMEOUT_SECONDS",
    )
    source_fetch_max_bytes: int = Field(
        default=2_000_000,
        alias="FX_SOURCE_FETCH_MAX_BYTES",
    )

    _generated_session_secret: str = PrivateAttr(default_factory=lambda: token_urlsafe(32))

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        populate_by_name=True,
        extra="ignore",
    )

    @property
    def static_dir(self) -> Path:
        return BASE_DIR / "app" / "static"

    @property
    def templates_dir(self) -> Path:
        return BASE_DIR / "app" / "templates"

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def storage_dirs(self) -> tuple[Path, ...]:
        return (
            self.data_dir,
            self.raw_data_dir,
            self.normalized_data_dir,
            self.evidence_dir,
            self.reports_dir,
            self.exports_dir,
            self.log_dir,
        )

    @property
    def allowed_hosts(self) -> list[str]:
        hosts = [host.strip() for host in self.allowed_hosts_raw.split(",") if host.strip()]
        return hosts or ["127.0.0.1", "localhost", "testserver"]

    @property
    def secure_cookies(self) -> bool:
        if self.secure_cookies_raw is not None:
            return self.secure_cookies_raw
        return self.environment.casefold() in {"production", "staging"}

    @property
    def session_secret(self) -> str:
        if self.session_secret_raw:
            return self.session_secret_raw
        if self.environment.casefold() in {"production", "staging"}:
            raise ValueError(
                "FX_SESSION_SECRET must be set when FX_ENVIRONMENT is production or staging."
            )
        return self._generated_session_secret


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
