"""Configuration.

Defaults are chosen so that ``uvicorn playslot.main:app`` works on a fresh machine with
no environment set at all — a café counter PC is not a place to debug missing variables.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PLAYSLOT_", env_file=".env")

    #: Identifies this venue in every row. Matters only once cloud sync is on, but it is
    #: written from day one so enabling sync later is not a backfill.
    venue_id: str = "venue-local"

    #: SQLite by the architecture's reasoning: a counter PC should not run a database
    #: service someone can stop. Relative path, resolved against the working directory.
    database_url: str = "sqlite:///./data/playslot.db"

    #: How often the session engine recomputes the floor. One second is comfortably
    #: below the resolution anyone perceives and costs nothing on this data volume.
    tick_seconds: float = 1.0

    #: Remaining seconds at which the amber warning fires. The architecture says
    #: exactly 300.
    warning_seconds: int = 300

    #: How long a scheduled booking is held before it is released as a no-show.
    no_show_timeout_minutes: int = 15

    #: Hour the business day rolls over. A session starting at 11pm belongs to that
    #: evening's shift report, not the next morning's.
    business_day_starts_hour: int = 6

    echo_sql: bool = False


settings = Settings()
