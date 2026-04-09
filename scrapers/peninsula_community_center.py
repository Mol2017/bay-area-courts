"""Scraper for Peninsula Community Center (Redwood City) basketball schedule.

Source page (Mindbody Classic, basketball tab):
  https://clients.mindbodyonline.com/classic/mainclass?studioid=782582&fl=true&tabID=109

Site ID 782582 is the studio identifier for Peninsula Community Center.
Without it, the URL the user provided redirects to Mindbody's site picker.

The basketball tab renders the full current week (Mon–Sun) inline. The DOM
shape is:

    .classSchedule-mainTable-loaded
      .header                       "Mon April 6, 2026"
      .row                          "Basketball"     (category label, ignored)
      .evenRow.row / .oddRow.row    one session
        .col-1 .col-first           "11:00 am PDT"
        .col-2 .col (1)             <a class="modalClassDesc">Open Gym</a>
        .col-2 .col (2)             "Center"        location part 1
        .col-2 .col (3)             "Gym"           location part 2
        .col-2 .col (4)             "1 hour"        duration

We walk the children in order, tracking the current day, and emit one Session
per session row. End time = start + parsed duration.
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

SOURCE_NAME = "peninsula_community_center"
# First-load URL (sets studio context, then internally redirects).
PRIME_URL = (
    "https://clients.mindbodyonline.com/classic/mainclass"
    "?studioid=782582&fl=true&tabID=109"
)
# Direct schedule URL — only renders the basketball tab once the studio
# context has been primed by visiting PRIME_URL once first.
SOURCE_URL = (
    "https://clients.mindbodyonline.com/classic/mainclass"
    "?studioid=782582&tabID=109"
)
VENUE = "Peninsula Community Center"
ADDRESS = "3623 Jefferson Avenue, Redwood City, CA 94062"


def _extract_rows(page) -> list[dict]:
    """Pull a flat list of records from the schedule DOM via JS."""
    return page.evaluate(
        """
        () => {
          const root = document.querySelector('.classSchedule-mainTable-loaded');
          if (!root) return [];
          const out = [];
          let currentDay = null;
          for (const el of root.children) {
            const cls = el.className || '';
            const text = (el.innerText || '').trim();
            if (cls === 'header') {
              currentDay = text;             // "Mon April 6, 2026"
              continue;
            }
            if (!cls.includes('evenRow') && !cls.includes('oddRow')) {
              // Plain ".row" rows are category labels like "Basketball".
              continue;
            }
            const time = el.querySelector('.col-1 .col-first')?.innerText?.trim() || '';
            const cols = [...el.querySelectorAll('.col-2 .col')].map(c =>
              (c.innerText || '').trim()
            );
            // cols = [activity, location1, location2, duration]
            out.push({
              day: currentDay,
              time,                          // "11:00 am PDT"
              activity: cols[0] || '',
              loc1: cols[1] || '',
              loc2: cols[2] || '',
              duration: cols[3] || '',
            });
          }
          return out;
        }
        """
    )


# ── parsing helpers ───────────────────────────────────────────────────────

_DAY_RE = re.compile(
    r"^(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+(\w+)\s+(\d{1,2}),\s+(\d{4})$"
)
_MONTHS = {
    m: i + 1
    for i, m in enumerate(
        [
            "January",
            "February",
            "March",
            "April",
            "May",
            "June",
            "July",
            "August",
            "September",
            "October",
            "November",
            "December",
        ]
    )
}
_TIME_RE = re.compile(r"(\d{1,2}):(\d{2})\s*(am|pm)", re.IGNORECASE)
_DURATION_RE = re.compile(
    r"(?:(\d+)\s*hour)?\s*(?:(\d+)\s*min)?", re.IGNORECASE
)


def parse_day(day_text: str) -> datetime | None:
    """'Mon April 6, 2026' → date in Pacific (midnight)."""
    m = _DAY_RE.match(day_text or "")
    if not m:
        return None
    month_name, day, year = m.groups()
    month = _MONTHS.get(month_name)
    if not month:
        return None
    return datetime(int(year), month, int(day), tzinfo=PACIFIC)


def parse_start(date_midnight: datetime, time_text: str) -> datetime | None:
    """Combine a midnight date with '11:00 am PDT' → tz-aware datetime."""
    m = _TIME_RE.search(time_text or "")
    if not m:
        return None
    hh, mm, ampm = int(m.group(1)), int(m.group(2)), m.group(3).lower()
    if ampm == "pm" and hh != 12:
        hh += 12
    elif ampm == "am" and hh == 12:
        hh = 0
    return date_midnight.replace(hour=hh, minute=mm)


def parse_duration_minutes(text: str) -> int | None:
    """'1 hour' → 60, '1 hour 30 min' → 90, '45 min' → 45, '' → None."""
    if not text:
        return None
    m = _DURATION_RE.search(text)
    if not m:
        return None
    hours = int(m.group(1) or 0)
    mins = int(m.group(2) or 0)
    total = hours * 60 + mins
    return total or None


def is_basketball(activity: str) -> bool:
    a = (activity or "").lower()
    return "basketball" in a or "open gym" in a or "pick-up" in a or "pickup" in a


def scrape() -> ScrapeResult:
    result = ScrapeResult(source=SOURCE_NAME, source_url=SOURCE_URL)
    window_start, window_end = data_window_range()
    print(
        f"[{SOURCE_NAME}] window {window_start.date()} – {window_end.date()}",
        file=sys.stderr,
    )

    with sync_playwright() as pw:
        # Mindbody Classic accepts the bundled Chromium fine, but using the
        # system Chrome channel keeps us consistent with the other scrapers.
        browser = pw.chromium.launch(headless=True, channel="chrome")
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            extra_http_headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        page = ctx.new_page()
        # Step 1: prime the studio context. The fl=true URL kicks off a JS
        # redirect chain that ends on a schedule shell with no rows.
        polite_goto(page, PRIME_URL, wait_until="commit")
        try:
            page.wait_for_selector(
                ".classSchedule-mainTable-loaded .header",
                timeout=30000,
                state="attached",
            )
        except Exception as e:  # noqa: BLE001
            print(f"[{SOURCE_NAME}] prime step failed: {e}", file=sys.stderr)
            browser.close()
            return result

        # Step 2: re-navigate to the basketball tab now that the studio
        # context is set. This load actually contains the session rows.
        polite_goto(page, SOURCE_URL, wait_until="commit")
        try:
            page.wait_for_selector(
                ".classSchedule-mainTable-loaded .evenRow, "
                ".classSchedule-mainTable-loaded .oddRow",
                timeout=30000,
                state="attached",
            )
        except Exception as e:  # noqa: BLE001
            print(f"[{SOURCE_NAME}] schedule rows never appeared: {e}", file=sys.stderr)
            browser.close()
            return result

        rows: list[dict] = list(_extract_rows(page))
        print(f"[{SOURCE_NAME}] week 1: {len(rows)} rows", file=sys.stderr)

        # Step 3: click the right-arrow next to "Week" to advance to next week.
        # The click triggers a full page navigation to a fresh `mainclass` URL
        # that renders the following Mon–Sun. We rate-limit it ourselves
        # because polite_goto only wraps page.goto.
        import time as _time

        try:
            _time.sleep(1.0)  # honor REQUEST_DELAY_SECONDS between requests
            page.click("#week-arrow-r", timeout=10000)
            page.wait_for_load_state("domcontentloaded", timeout=30000)
            page.wait_for_selector(
                ".classSchedule-mainTable-loaded .evenRow, "
                ".classSchedule-mainTable-loaded .oddRow",
                timeout=30000,
                state="attached",
            )
            week2_rows = list(_extract_rows(page))
            print(f"[{SOURCE_NAME}] week 2: {len(week2_rows)} rows", file=sys.stderr)
            rows.extend(week2_rows)
        except Exception as e:  # noqa: BLE001
            print(
                f"[{SOURCE_NAME}] failed to advance to next week (keeping week 1 only): {e}",
                file=sys.stderr,
            )

        browser.close()

    print(f"[{SOURCE_NAME}] {len(rows)} raw rows", file=sys.stderr)

    for row in rows:
        if not is_basketball(row.get("activity", "")):
            continue
        date_midnight = parse_day(row.get("day", ""))
        if not date_midnight:
            continue
        start = parse_start(date_midnight, row.get("time", ""))
        if not start:
            continue
        duration_min = parse_duration_minutes(row.get("duration", ""))
        if not duration_min:
            duration_min = 60  # sensible default — Mindbody usually shows hours
        end = start + timedelta(minutes=duration_min)
        if not in_data_window(start):
            continue

        loc_bits = [b for b in (row.get("loc1"), row.get("loc2")) if b]
        room = " ".join(loc_bits) if loc_bits else None

        try:
            result.add_session(
                Session(
                    venue=VENUE,
                    start=start.isoformat(timespec="minutes"),
                    end=end.isoformat(timespec="minutes"),
                    activity=row["activity"],
                    address=ADDRESS,
                    # Hard-coded from venue staff, 2026-04 — not advertised on
                    # the Mindbody schedule page itself.
                    cost="$55 per person (drop-in)",
                    notes=f"Room: {room}" if room else None,
                    source_event_url=SOURCE_URL,
                )
            )
        except Exception as e:  # noqa: BLE001
            print(f"[{SOURCE_NAME}] invalid row {row}: {e}", file=sys.stderr)

    result.sessions.sort(key=lambda s: s.start)
    print(
        f"[{SOURCE_NAME}] kept {len(result.sessions)} basketball sessions",
        file=sys.stderr,
    )
    return result


if __name__ == "__main__":
    out = write_result(scrape())
    print(f"wrote {out}")
