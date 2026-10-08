"""Runtime settings, all from environment variables (12-factor style)."""
from __future__ import annotations

import os
from dataclasses import dataclass


def _float(name: str, default: float) -> float:
    v = os.environ.get(name)
    return float(v) if v not in (None, "") else default


def _int(name: str, default: int) -> int:
    v = os.environ.get(name)
    return int(v) if v not in (None, "") else default


def _db_url(raw: str) -> str:
    # DigitalOcean (and Heroku-style) URLs say postgres:// or postgresql://;
    # SQLAlchemy needs the driver spelled out to use psycopg 3.
    for prefix in ("postgres://", "postgresql://"):
        if raw.startswith(prefix):
            return "postgresql+psycopg://" + raw[len(prefix):]
    return raw


@dataclass(frozen=True)
class Settings:
    database_url: str
    ingest_token: str
    dashboard_user: str
    dashboard_password: str
    # How temp/hum are derived: "router" (trust router-decoded values),
    # "raw_be" or "raw_le" (decode from the 16-byte raw payload).
    decode_mode: str
    max_records_per_post: int
    max_body_bytes: int
    stale_tag_minutes: float
    silent_router_minutes: float
    clock_skew_warn_seconds: float
    temp_min: float
    temp_max: float
    hum_min: float
    hum_max: float
    require_postgres: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        mode = os.environ.get("DECODE_MODE", "router").strip().lower()
        if mode not in ("router", "raw_be", "raw_le"):
            raise ValueError(f"DECODE_MODE must be router, raw_be or raw_le, got {mode!r}")
        return cls(
            database_url=_db_url(os.environ.get("DATABASE_URL", "sqlite:///./data/mesh.db")),
            ingest_token=os.environ.get("INGEST_TOKEN", ""),
            dashboard_user=os.environ.get("DASHBOARD_USER", "admin"),
            dashboard_password=os.environ.get("DASHBOARD_PASSWORD", ""),
            decode_mode=mode,
            max_records_per_post=_int("MAX_RECORDS_PER_POST", 1000),
            max_body_bytes=_int("MAX_BODY_BYTES", 2_000_000),
            stale_tag_minutes=_float("STALE_TAG_MINUTES", 30),
            silent_router_minutes=_float("SILENT_ROUTER_MINUTES", 5),
            clock_skew_warn_seconds=_float("CLOCK_SKEW_WARN_SECONDS", 120),
            temp_min=_float("TEMP_MIN", -40),
            temp_max=_float("TEMP_MAX", 85),
            hum_min=_float("HUM_MIN", 0),
            hum_max=_float("HUM_MAX", 100),
            require_postgres=os.environ.get("REQUIRE_POSTGRES", "").strip().lower() in ("1", "true", "yes"),
        )
