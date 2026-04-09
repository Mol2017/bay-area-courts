"""Unified data schema every scraper must conform to.

Each scraper produces a single JSON file in `data/raw/<source>.json` with this
top-level shape:

    {
      "source":      str,            # required, snake_case slug, must match filename
      "source_url":  str,            # required, the page the data was scraped from
      "scraped_at":  str (ISO-8601), # set by ScrapeResult.to_dict()
      "sessions":    [Session, ...]  # 0 or more
    }

A Session represents one drop-in time window at one venue:

    Required:
      venue      str           Human-readable venue name
      start      str (ISO-8601 with timezone offset, minutes precision)
      end        str (ISO-8601 with timezone offset, minutes precision)

    Optional (use None / omit if unknown):
      activity            str    Defaults to "Basketball"
      address             str    Street address
      lat                 float
      lon                 float
      cost                str    Free-form, e.g. "$5 adult / $1 youth"
      notes               str    Anything the user should know
      source_event_url    str    Per-session deep link if available
      courts              list   Per-court breakdown for venues with multiple
                                 courts (e.g. Newark, Arrillaga). Each entry:
                                 {"name": "Court 1",
                                  "windows": ["6:00 AM–5:00 PM", "6:00 PM–9:00 PM"]}

`Session.validate()` enforces the required fields and date ordering. Scrapers
should call it (or `ScrapeResult.add_session()`) before producing output so
malformed records are caught locally instead of in the merge step.
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

PACIFIC = ZoneInfo("America/Los_Angeles")
REPO_ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = REPO_ROOT / "data" / "raw"

SCHEMA_VERSION = 1


class SchemaError(ValueError):
    """Raised when a Session or ScrapeResult fails validation."""


@dataclass
class Session:
    venue: str
    start: str
    end: str
    activity: str = "Basketball"
    address: str | None = None
    lat: float | None = None
    lon: float | None = None
    cost: str | None = None
    notes: str | None = None
    source_event_url: str | None = None
    courts: list[dict] | None = None

    def validate(self) -> None:
        if not self.venue:
            raise SchemaError("Session.venue is required")
        if not self.start or not self.end:
            raise SchemaError(f"Session at {self.venue!r} missing start/end")
        try:
            s = datetime.fromisoformat(self.start)
            e = datetime.fromisoformat(self.end)
        except ValueError as exc:
            raise SchemaError(f"Session at {self.venue!r} bad ISO datetime: {exc}")
        if s.tzinfo is None or e.tzinfo is None:
            raise SchemaError(
                f"Session at {self.venue!r} start/end must include a timezone offset"
            )
        if e <= s:
            raise SchemaError(f"Session at {self.venue!r} ends before/at its start")


@dataclass
class ScrapeResult:
    source: str
    source_url: str
    sessions: list[Session] = field(default_factory=list)

    def add_session(self, session: Session) -> None:
        session.validate()
        self.sessions.append(session)

    def validate(self) -> None:
        if not self.source or not self.source.replace("_", "").isalnum():
            raise SchemaError(
                f"ScrapeResult.source must be a snake_case slug, got {self.source!r}"
            )
        if not self.source_url:
            raise SchemaError("ScrapeResult.source_url is required")
        for s in self.sessions:
            s.validate()

    def to_dict(self) -> dict:
        self.validate()
        return {
            "schema_version": SCHEMA_VERSION,
            "source": self.source,
            "source_url": self.source_url,
            "scraped_at": datetime.now(timezone.utc)
            .astimezone(PACIFIC)
            .isoformat(timespec="seconds"),
            "sessions": [asdict(s) for s in self.sessions],
        }


# ── adjacent-session merging ──────────────────────────────────────────────

_ACTIVITY_PUNCT_RE = re.compile(r"[^\w\s]")
_WHITESPACE_RE = re.compile(r"\s+")


def _activity_key(activity: str | None) -> str:
    """Normalize an activity string for equality comparison.

    "RMCC Drop In Basketball" and "RMCC Drop-In Basketball" should both map
    to the same key so that back-to-back sessions with subtly different
    spellings still merge.
    """
    if not activity:
        return ""
    a = activity.lower()
    a = _ACTIVITY_PUNCT_RE.sub(" ", a)
    a = _WHITESPACE_RE.sub(" ", a).strip()
    return a


def _venue_key(venue: str | None) -> str:
    return (venue or "").strip().lower()


def merge_adjacent_sessions(sessions: list[Session]) -> list[Session]:
    """Combine sessions at the same venue/activity that touch or overlap.

    Two sessions [s1, e1] and [s2, e2] merge into [s1, max(e1, e2)] when:
      - venue strings match (case-insensitive)
      - activities normalize to the same key (see `_activity_key`)
      - s2 <= e1  (touching counts — back-to-back becomes one block)

    Non-time fields are inherited from the earlier session. The original
    list is not mutated; the result is a fresh list of fresh Session
    instances and is sorted by start time.
    """
    if not sessions:
        return []

    groups: dict[tuple[str, str], list[Session]] = {}
    for s in sessions:
        groups.setdefault((_venue_key(s.venue), _activity_key(s.activity)), []).append(s)

    out: list[Session] = []
    for group in groups.values():
        group.sort(key=lambda x: x.start)
        merged: list[Session] = [Session(**asdict(group[0]))]
        for cur in group[1:]:
            last = merged[-1]
            try:
                last_end = datetime.fromisoformat(last.end)
                cur_start = datetime.fromisoformat(cur.start)
                cur_end = datetime.fromisoformat(cur.end)
            except ValueError:
                # Bad data — keep them separate rather than crash here.
                merged.append(Session(**asdict(cur)))
                continue
            if cur_start <= last_end:
                if cur_end > last_end:
                    last.end = cur.end
                # else: cur is entirely contained → drop it
            else:
                merged.append(Session(**asdict(cur)))
        out.extend(merged)

    out.sort(key=lambda x: x.start)
    return out


def write_result(result: ScrapeResult) -> Path:
    """Validate, merge adjacent sessions, and write to data/raw/<source>.json.

    Adjacent-session merging happens here so every scraper benefits without
    having to remember to call it. Scrapers that explicitly need raw,
    un-merged output can call `merge_adjacent_sessions` themselves and
    bypass this helper.
    """
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    before = len(result.sessions)
    result.sessions = merge_adjacent_sessions(result.sessions)
    after = len(result.sessions)
    if after != before:
        print(
            f"[{result.source}] merged adjacent sessions: {before} → {after}",
            file=sys.stderr,
        )
    out = RAW_DIR / f"{result.source}.json"
    out.write_text(json.dumps(result.to_dict(), indent=2))
    return out
