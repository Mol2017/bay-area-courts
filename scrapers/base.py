"""Helpers shared across scrapers.

This module is utilities only — the canonical data shape lives in
``scrapers/schema.py`` and is re-exported here so individual scrapers can
import everything they need from one place.

What lives here:
  * Data window math (``data_window_range`` / ``in_data_window``).
    The "data window" is a 2-week range starting on the current week's
    Monday in Pacific time. Both the merge step and the calendar UI assume
    this is what every scraper produces.
  * ``parse_iso`` — tolerant ISO-8601 parser that defaults naive datetimes
    to Pacific.
  * ``polite_goto`` — a thin ``page.goto`` wrapper that throttles to at
    most one HTTP request per ``REQUEST_DELAY_SECONDS`` so we don't hammer
    the city websites.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta

# Re-export schema types so individual scrapers can do `from base import ...`
# without caring about the split. New code may also import from `schema`
# directly.
from schema import (  # noqa: F401  (re-export)
    PACIFIC,
    RAW_DIR,
    REPO_ROOT,
    SCHEMA_VERSION,
    SchemaError,
    ScrapeResult,
    Session,
    write_result,
)

# Minimum seconds to wait between any two HTTP requests issued by a scraper.
REQUEST_DELAY_SECONDS = 1.0
_last_request_at: float = 0.0

# How many weeks of forward-looking data we keep beyond the current week.
# WEEKS_AHEAD = 1 → a 2-week window covering current + next week. The merge
# step in scripts/merge.py mirrors this constant; keep them in sync.
WEEKS_AHEAD = 1


def data_window_range(now: datetime | None = None) -> tuple[datetime, datetime]:
    """Return ``(this Monday 00:00 PT, Monday after WEEKS_AHEAD weeks 00:00 PT)``.

    With WEEKS_AHEAD = 1 this returns a 14-day window. Scrapers filter their
    sessions through ``in_data_window`` so older or further-future events
    never reach ``data/raw/*.json``.
    """
    now = (now or datetime.now(PACIFIC)).astimezone(PACIFIC)
    monday = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return monday, monday + timedelta(days=7 * (1 + WEEKS_AHEAD))


def in_data_window(dt: datetime) -> bool:
    """True if ``dt`` falls inside the current data window."""
    start, end = data_window_range()
    return start <= dt.astimezone(PACIFIC) < end


def parse_iso(s: str) -> datetime:
    """Parse an ISO-8601 string. Naive inputs are treated as Pacific."""
    s = s.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=PACIFIC)
    return dt


def polite_goto(page, url: str, **kwargs):
    """``page.goto`` wrapper that enforces ≥REQUEST_DELAY_SECONDS between
    any two scraper requests.

    Use this in every scraper instead of calling ``page.goto`` directly so
    we stay friendly to the city websites we scrape from.
    """
    global _last_request_at
    elapsed = time.monotonic() - _last_request_at
    if elapsed < REQUEST_DELAY_SECONDS:
        time.sleep(REQUEST_DELAY_SECONDS - elapsed)
    try:
        return page.goto(url, **kwargs)
    finally:
        _last_request_at = time.monotonic()
