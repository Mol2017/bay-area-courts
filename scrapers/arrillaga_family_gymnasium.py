"""Scraper for Arrillaga Family Gymnasium (City of Menlo Park) via CatchCorner.

Source page (CatchCorner embedded facility view):
  https://www.catchcorner.com/facility-page/embedded/rental/arrillaga-family-gymnasium-city-of-menlo-park/Basketball

Important context — these are NOT free drop-in sessions. CatchCorner is a
court-rental marketplace; each "tile" in the listing is a *paid bookable
slot* ($88/hr) at one of two basketball courts. The site advertises every
2-hour window the court is free, stepping the start time every 30 minutes,
so a continuously open Friday morning (e.g. 7:30 AM – 12:00 PM) shows up as
seven overlapping 2-hour tiles. We rely on the schema-level
`merge_adjacent_sessions` helper to collapse those overlapping rentals into
one continuous availability block per court.

DOM shape (Angular SPA):
  app-listing-tile
    .listing-tile__weekday    "THU"
    .listing-tile__date       "Apr 09"
    .listing-tile__time       "5:30pm - 7:30pm"
    .listing-tile__tag        "Court 2 (Basketball) (84ft x 50ft)"
    .listing-tile__price      "$88.00"

The list is virtualized / lazy-loaded; we scroll the inner scrollable
container until the tile count stops growing.
"""
from __future__ import annotations

import re
import sys
from datetime import datetime, timedelta

from playwright.sync_api import sync_playwright

from base import (
    PACIFIC,
    ScrapeResult,
    Session,
    data_window_range,
    in_data_window,
    polite_goto,
    write_result,
)

SOURCE_NAME = "arrillaga_family_gymnasium"
SOURCE_URL = (
    "https://www.catchcorner.com/facility-page/embedded/rental/"
    "arrillaga-family-gymnasium-city-of-menlo-park/Basketball"
)
VENUE = "Arrillaga Family Gymnasium"
ADDRESS = "600 Alma St, Menlo Park, CA 94025"

# Scrolling: cap iterations so a misbehaving page can't loop forever.
MAX_SCROLL_ITERATIONS = 30
SCROLL_PAUSE_MS = 1200


# ── DOM extraction ────────────────────────────────────────────────────────


def _scroll_and_extract(page) -> list[dict]:
    """Scroll the listing until tile count stabilizes, then return tiles."""
    return page.evaluate(
        f"""
        async () => {{
          // Find the scrollable inner container (Angular CDK virtual scroll
          // or any plain overflow:auto element).
          function findScrollables() {{
            return [...document.querySelectorAll('*')].filter(el => {{
              const cs = getComputedStyle(el);
              return el.scrollHeight > el.clientHeight + 50
                  && /auto|scroll/.test(cs.overflowY);
            }});
          }}

          let last = -1;
          let cur = document.querySelectorAll('app-listing-tile').length;
          let iter = 0;
          while (cur !== last && iter < {MAX_SCROLL_ITERATIONS}) {{
            last = cur;
            for (const s of findScrollables()) s.scrollTop = s.scrollHeight;
            window.scrollTo(0, document.body.scrollHeight);
            await new Promise(r => setTimeout(r, {SCROLL_PAUSE_MS}));
            cur = document.querySelectorAll('app-listing-tile').length;
            iter++;
          }}

          return [...document.querySelectorAll('app-listing-tile')].map(t => ({{
            weekday: t.querySelector('.listing-tile__weekday')?.innerText?.trim() || '',
            date:    t.querySelector('.listing-tile__date')?.innerText?.trim() || '',
            time:    t.querySelector('.listing-tile__time')?.innerText?.trim() || '',
            tag:     t.querySelector('.listing-tile__tag')?.innerText?.trim() || '',
            price:   t.querySelector('.listing-tile__price')?.innerText?.trim() || '',
          }}));
        }}
        """
    )


# ── parsing ───────────────────────────────────────────────────────────────

_MONTHS_ABBR = {
    m: i + 1
    for i, m in enumerate(
        [
            "Jan",
            "Feb",
            "Mar",
            "Apr",
            "May",
            "Jun",
            "Jul",
            "Aug",
            "Sep",
            "Oct",
            "Nov",
            "Dec",
        ]
    )
}
_DATE_RE = re.compile(r"^([A-Za-z]{3})\s+(\d{1,2})$")
# "5:30pm - 7:30pm"  or  "10:00am - 12:00pm"
_TIME_RANGE_RE = re.compile(
    r"(\d{1,2}):(\d{2})\s*(am|pm)\s*-\s*(\d{1,2}):(\d{2})\s*(am|pm)",
    re.IGNORECASE,
)
# "Court 2 (Basketball) (84ft x 50ft)"  →  "Court 2"
_COURT_RE = re.compile(r"^(Court\s+\d+)", re.IGNORECASE)


def _to_24h(hh: int, mm: int, ampm: str) -> tuple[int, int]:
    ampm = ampm.lower()
    if ampm == "pm" and hh != 12:
        hh += 12
    elif ampm == "am" and hh == 12:
        hh = 0
    return hh, mm


def _infer_year(month: int, day: int, today: datetime) -> int:
    """Choose a year so the resulting date is closest to today (±6 months).

    Handles year boundaries: if it's late December and the page lists
    "Jan 02", that's next year, not this one.
    """
    for year in (today.year - 1, today.year, today.year + 1):
        try:
            cand = datetime(year, month, day, tzinfo=PACIFIC)
        except ValueError:
            continue
        if abs((cand - today).days) <= 180:
            return year
    return today.year


def parse_date(date_text: str, today: datetime) -> datetime | None:
    """'Apr 09' → midnight Pacific on the inferred year."""
    m = _DATE_RE.match(date_text or "")
    if not m:
        return None
    month = _MONTHS_ABBR.get(m.group(1).title())
    if not month:
        return None
    day = int(m.group(2))
    year = _infer_year(month, day, today)
    try:
        return datetime(year, month, day, tzinfo=PACIFIC)
    except ValueError:
        return None


def parse_time_range(
    date_midnight: datetime, time_text: str
) -> tuple[datetime, datetime] | None:
    m = _TIME_RANGE_RE.search(time_text or "")
    if not m:
        return None
    sh, sm = _to_24h(int(m.group(1)), int(m.group(2)), m.group(3))
    eh, em = _to_24h(int(m.group(4)), int(m.group(5)), m.group(6))
    start = date_midnight.replace(hour=sh, minute=sm)
    end = date_midnight.replace(hour=eh, minute=em)
    if end <= start:
        # Handles a hypothetical overnight window like 11pm–1am.
        end += timedelta(days=1)
    return start, end


def parse_court(tag_text: str) -> str:
    m = _COURT_RE.match(tag_text or "")
    return m.group(1).title() if m else "Court"


# ── per-day Court 1 + Court 2 union (same pattern as Newark) ──────────────


def _to_minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _from_minutes(total: int) -> str:
    return f"{total // 60:02d}:{total % 60:02d}"


def _format_pretty(hhmm: str) -> str:
    h, m = (int(p) for p in hhmm.split(":"))
    suffix = "AM" if h < 12 else "PM"
    h12 = h % 12 or 12
    return f"{h12}:{m:02d} {suffix}"


def _format_range_pretty(start: str, end: str) -> str:
    return f"{_format_pretty(start)}–{_format_pretty(end)}"


def _merge_intervals(
    intervals: list[tuple[int, int]],
) -> list[tuple[int, int]]:
    if not intervals:
        return []
    intervals = sorted(intervals)
    out = [intervals[0]]
    for s, e in intervals[1:]:
        last_s, last_e = out[-1]
        if s <= last_e:
            out[-1] = (last_s, max(last_e, e))
        else:
            out.append((s, e))
    return out


def _build_court_breakdown_struct(
    per_court: dict[str, list[tuple[str, str]]],
) -> list[dict]:
    """[{name: 'Court 1', windows: ['8:00 AM–12:00 PM']}, ...]"""
    out: list[dict] = []
    for court_name in sorted(per_court.keys()):
        ranges = sorted(per_court[court_name])
        merged = _merge_intervals(
            [(_to_minutes(s), _to_minutes(e)) for s, e in ranges]
        )
        windows = [
            _format_range_pretty(_from_minutes(s), _from_minutes(e))
            for s, e in merged
        ]
        out.append({"name": court_name, "windows": windows})
    return out


def _build_day_sessions(
    date_iso: str,
    per_court: dict[str, list[tuple[str, str]]],
    price_text: str,
) -> list[Session]:
    """Compute the union of all courts' windows for one day; emit one Session
    per merged window.
    """
    if not per_court:
        return []

    # Union across courts.
    all_intervals: list[tuple[int, int]] = []
    for windows in per_court.values():
        for s, e in windows:
            all_intervals.append((_to_minutes(s), _to_minutes(e)))
    union = _merge_intervals(all_intervals)

    courts_struct = _build_court_breakdown_struct(per_court)
    breakdown_text = ". ".join(
        f"{entry['name']}: " + ", ".join(entry["windows"])
        for entry in courts_struct
    ) + "."
    courts_label = (
        " + ".join(sorted(per_court.keys())) if len(per_court) > 1 else next(iter(per_court))
    )
    notes = (
        f"Combined {courts_label} availability at Arrillaga Family Gymnasium. "
        f"{breakdown_text} CatchCorner lists these as $88/hr rentals in 2-hour "
        f"blocks, but if no one books a slot the court is open as free drop-in "
        f"during the same window."
    )

    y, m, d = (int(p) for p in date_iso.split("-"))
    sessions: list[Session] = []
    for start_min, end_min in union:
        start = datetime(y, m, d, start_min // 60, start_min % 60, tzinfo=PACIFIC)
        end = datetime(y, m, d, end_min // 60, end_min % 60, tzinfo=PACIFIC)
        if end <= start:
            end += timedelta(days=1)
        sessions.append(
            Session(
                venue=VENUE,
                start=start.isoformat(timespec="minutes"),
                end=end.isoformat(timespec="minutes"),
                activity="Court Rental",
                address=ADDRESS,
                # Hard-coded from venue staff, 2026-04: even though
                # CatchCorner lists this as a $88/hr rental, the slot is open
                # as free drop-in unless somebody actually books it.
                cost=f"Free drop-in (or {price_text}/hr to reserve)",
                notes=notes,
                source_event_url=SOURCE_URL,
                courts=courts_struct,
            )
        )
    return sessions


# ── orchestration ─────────────────────────────────────────────────────────


def scrape() -> ScrapeResult:
    result = ScrapeResult(source=SOURCE_NAME, source_url=SOURCE_URL)
    window_start, window_end = data_window_range()
    today = datetime.now(PACIFIC)
    print(
        f"[{SOURCE_NAME}] window {window_start.date()} – {window_end.date()}",
        file=sys.stderr,
    )

    with sync_playwright() as pw:
        # Bundled chromium is fine here, but we keep channel="chrome" for
        # consistency with the other scrapers.
        browser = pw.chromium.launch(headless=True, channel="chrome")
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
        )
        page = ctx.new_page()
        polite_goto(page, SOURCE_URL, wait_until="domcontentloaded")
        try:
            page.wait_for_selector(
                "app-listing-tile", timeout=30000, state="attached"
            )
        except Exception as e:  # noqa: BLE001
            print(f"[{SOURCE_NAME}] no listing tiles: {e}", file=sys.stderr)
            browser.close()
            return result

        tiles = _scroll_and_extract(page)
        browser.close()

    print(f"[{SOURCE_NAME}] {len(tiles)} raw tiles after scroll", file=sys.stderr)

    # Bucket every tile by (date, court). Then for each date, union the
    # courts' windows so the venue shows one combined availability block.
    per_day: dict[str, dict[str, set[tuple[str, str]]]] = {}
    price_text = "$88.00"

    for tile in tiles:
        date_midnight = parse_date(tile.get("date", ""), today)
        if not date_midnight:
            continue
        rng = parse_time_range(date_midnight, tile.get("time", ""))
        if not rng:
            continue
        start, end = rng
        if not in_data_window(start):
            continue
        court = parse_court(tile.get("tag", ""))
        if tile.get("price"):
            price_text = tile["price"].strip()
        date_iso = start.date().isoformat()
        per_day.setdefault(date_iso, {}).setdefault(court, set()).add(
            (start.strftime("%H:%M"), end.strftime("%H:%M"))
        )

    raw_tile_count = sum(
        len(ws) for courts in per_day.values() for ws in courts.values()
    )
    union_session_count = 0

    for date_iso in sorted(per_day.keys()):
        per_court_lists = {c: sorted(ws) for c, ws in per_day[date_iso].items()}
        for session in _build_day_sessions(date_iso, per_court_lists, price_text):
            try:
                result.add_session(session)
                union_session_count += 1
            except Exception as e:  # noqa: BLE001
                print(f"[{SOURCE_NAME}] invalid session: {e}", file=sys.stderr)

    result.sessions.sort(key=lambda s: s.start)
    print(
        f"[{SOURCE_NAME}] kept {union_session_count} merged sessions "
        f"({raw_tile_count} raw tiles in data window)",
        file=sys.stderr,
    )
    return result


if __name__ == "__main__":
    out = write_result(scrape())
    print(f"wrote {out}")
