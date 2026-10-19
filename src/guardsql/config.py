from functools import lru_cache

from psycopg.conninfo import make_conninfo
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings, loaded from environment variables and .env."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "local"
    log_level: str = "INFO"

    # Database (read-only role only: the app never holds admin credentials)
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "shop"
    postgres_schema: str = "shop"
    postgres_ro_user: str = "guardsql_ro"
    postgres_ro_password: str = ""
    # The role is created with CONNECTION LIMIT 10, so the pool may never exceed that.
    pool_max_size: int = Field(default=5, ge=1, le=10)

    # Guardrails
    # Comma-separated. Compared case-insensitively against column names in generated SQL.
    restricted_columns: str = "email,phone,date_of_birth,salary"
    default_row_limit: int = Field(default=100, ge=1)
    max_row_limit: int = Field(default=1000, ge=1)
    max_sql_length: int = Field(default=4000, ge=100)
    # Application-side time budget for one query. Independent of the role's own default.
    statement_timeout_ms: int = Field(default=3000, ge=50)
    # Planner cost units (not seconds). Calibrate against your own data; see DECISIONS.md.
    max_plan_cost: float = Field(default=50_000.0, gt=0)

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

    @property
    def restricted_column_set(self) -> frozenset[str]:
        return frozenset(
            name.strip().lower() for name in self.restricted_columns.split(",") if name.strip()
        )

    @property
    def policy_limits(self) -> dict[str, int]:
        """Keyword arguments for SchemaSnapshot.to_policy()."""
        return {
            "default_limit": min(self.default_row_limit, self.max_row_limit),
            "max_limit": self.max_row_limit,
            "max_sql_length": self.max_sql_length,
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()
