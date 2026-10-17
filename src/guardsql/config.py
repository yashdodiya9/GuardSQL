from functools import lru_cache

from psycopg.conninfo import make_conninfo
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings, loaded from environment variables and .env."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "local"
    log_level: str = "INFO"

    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "shop"
    postgres_ro_user: str = "guardsql_ro"
    postgres_ro_password: str = ""

    @property
    def readonly_conninfo(self) -> str:
        """Connection string for the read-only role (the only one the executor may use)."""
        return make_conninfo(
            host=self.postgres_host,
            port=self.postgres_port,
            dbname=self.postgres_db,
            user=self.postgres_ro_user,
            password=self.postgres_ro_password,
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
