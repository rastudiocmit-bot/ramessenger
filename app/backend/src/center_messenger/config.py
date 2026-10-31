from pathlib import Path
from urllib.parse import urlsplit
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CM_", extra="ignore", hide_input_in_errors=True)

    environment: str = "development"
    db_host: str = "localhost"
    db_port: int = Field(default=5432, ge=1, le=65535)
    db_name: str = "center_messenger"
    db_user: str = "cm_app"
    db_password: SecretStr | None = None
    db_password_file: Path | None = None
    expected_db_role: str = "cm_app"
    db_connect_timeout: int = Field(default=3, ge=1, le=30)
    db_statement_timeout_ms: int = Field(default=2500, ge=100, le=30000)
    public_url: str = "http://localhost:8000"
    # Former public origins (comma-separated). After a domain move the MAX bot's old
    # webhook on them is removed when receiving is enabled again on the new domain.
    legacy_public_urls: str = "https://messenger.rastudio.org"
    bootstrap_token_file: Path | None = None
    connection_master_key_file: Path | None = None
    trust_proxy_ip: bool = False
    crm_sync_worker: bool = False
    max_worker: bool = False
    runtime_role: Literal['combined', 'api', 'max', 'telephony'] = 'combined'
    max_worker_key_file: Path | None = None
    telephony_worker_key_file: Path | None = None
    db_pool_size: int = Field(default=6, ge=1, le=8)
    db_max_overflow: int = Field(default=2, ge=0, le=3)
    session_hours: int = Field(default=1200, ge=1, le=8760)
    session_idle_minutes: int = Field(default=6000, ge=5, le=525600)

    @model_validator(mode="after")
    def check_password_source(self):
        if self.db_password is not None and self.db_password_file is not None:
            raise ValueError("Configure one database password source")
        return self

    @model_validator(mode="after")
    def check_public_url(self):
        url = urlsplit(self.public_url)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.path not in {"", "/"}
            or url.query
            or url.fragment
        ):
            raise ValueError("Public URL must be an HTTP(S) origin")
        if self.environment == "production" and url.scheme != "https":
            raise ValueError("Production public URL must use HTTPS")
        self.public_url = self.public_url.rstrip("/")
        return self

    @property
    def legacy_origins(self) -> list[str]:
        result = []
        for item in self.legacy_public_urls.split(","):
            url = urlsplit(item.strip())
            if url.scheme == "https" and url.hostname and not url.path.strip("/") and not url.query:
                origin = f"https://{url.netloc}"
                if origin != self.public_url and origin not in result:
                    result.append(origin)
        return result

    @property
    def cookie_name(self) -> str:
        return "__Host-cm_session" if self.public_url.startswith("https://") else "cm_session"

    def database_url(self) -> URL:
        password = self.db_password.get_secret_value() if self.db_password is not None else None
        if self.db_password_file is not None:
            password = self.db_password_file.read_text().rstrip("\r\n")
        if not password:
            raise ValueError("Database password is not configured")
        return URL.create(
            "postgresql+psycopg",
            username=self.db_user,
            password=password,
            host=self.db_host,
            port=self.db_port,
            database=self.db_name,
        )
