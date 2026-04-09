"""Scraper for Red Morton Community Center (Redwood City) Open Gym schedule.

Source page:
  https://www.redwoodcity.org/departments/parks-recreation-and-community-services/sports/open-gym

The page lists drop-in basketball, volleyball, badminton, and pickleball at
the Red Morton Community Center and the Armory Gym. We only collect
basketball.

Strategy:
1. Use Playwright with the system Chrome channel (the page is bot-walled to
   bundled Chromium and to plain HTTP).
2. Load the open-gym page; collect every distinct event link in the calendar
   widget whose label contains "Basketball".
3. Visit each event detail page and parse:
   - JSON-LD (`script[type="application/ld+json"]`) for venue/start/end
   - The descriptive `<p>` element under `<h1>OPEN GYM</h1>` which says
     either "Drop-In Basketball - Half Gym" or "Drop In Basketball - Full Gym"
4. For each event we know:
     • venue ("Red Morton Community Center" or "The Armory")
     • gym variant (Half / Full)
     • Number of courts open:
         RMCC + Half Gym → 1 court (Court 1)
         RMCC + Full Gym → 2 courts (Court 1 + Court 2)
         The Armory     → always 1 court (Court 1)
5. Bucket events by (venue, court_count) so back-to-back sessions of the
   same configuration merge cleanly. For each merged interval emit one
   Session with structured `courts` field for the popup breakdown.

All HTTP requests go through `polite_goto` so there is at least one second
between any two page loads.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from urllib.parse import urljoin

from playwright.sync_api import sync_playwright

from base import (
    PACIFIC,
    ScrapeResult,
    Session,
    data_window_range,
    in_data_window,
    parse_iso,
    polite_goto,
    write_result,
)

SOURCE_NAME = "red_morton_community_center"
SOURCE_URL = (
    "https://www.redwoodcity.org/departments/parks-recreation-and-community-services"
    "/sports/open-gym"
)
EVENT_HREF_RE = re.compile(r"/Home/Components/Calendar/Event/\d+/\d+")
GYM_VARIANT_RE = re.compile(r"\b(Half|Full)\s*Gym\b", re.IGNORECASE)
ARMORY_VENUE_HINT = "armory"


# ── stage 1: discover event URLs ──────────────────────────────────────────


def collect_basketball_event_urls(page) -> list[str]:
    polite_goto(page, SOURCE_URL, wait_until="domcontentloaded")
    page.wait_for_selector(
        "a[href*='/Home/Components/Calendar/Event/']",
        timeout=20000,
        state="attached",
    )

    hrefs: set[str] = set()
    for a in page.query_selector_all("a[href*='/Home/Components/Calendar/Event/']"):
        text = (a.inner_text() or "").strip()
        if "basketball" not in text.lower():
            continue
        href = a.get_attribute("href") or ""
        if EVENT_HREF_RE.search(href):
            hrefs.add(urljoin(SOURCE_URL, href))
    return sorted(hrefs)


# ── stage 2: parse one event detail page ─────────────────────────────────


def _extract_gym_variant(page) -> str | None:
    """Read the small `<p>` under the OPEN GYM heading.

    Examples we've seen on real pages:
      'Drop-In Basketball - Half Gym'
      'Drop In Basketball - Full Gym'
    Returns 'Half' or 'Full', or None if neither is found.
    """
    text = page.evaluate(
        """
        () => {
          const main = document.querySelector('main') || document.body;
          const ps = [...main.querySelectorAll('p')];
          for (const p of ps) {
            const t = (p.innerText || '').trim();
            if (/half\\s*gym|full\\s*gym/i.test(t)) return t;
          }
          return '';
        }
        """
    )
    m = GYM_VARIANT_RE.search(text or "")
    return m.group(1).title() if m else None


def parse_event_page(page, url: str) -> dict | None:
    """Visit one event detail page and return a raw event record.

    Returns:
        {venue, address, start, end, gym_variant, source_event_url}
        or None if the page didn't yield a usable record.
    """
    polite_goto(page, url, wait_until="domcontentloaded")
    ld_el = page.query_selector("script[type='application/ld+json']")
    if not ld_el:
        return None
    try:
        data = json.loads(ld_el.inner_text())
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None

    start_raw = data.get("startDate")
    end_raw = data.get("endDate")
    if not (start_raw and end_raw):
        return None
    start = parse_iso(start_raw)
    end = parse_iso(end_raw)
    if not in_data_window(start):
        return None

    location = data.get("location") or {}
    venue = location.get("name") or "Red Morton Community Center"
    address = location.get("address")

    gym_variant = _extract_gym_variant(page)  # 'Half' | 'Full' | None

    return {
        "venue": venue,
        "address": address,
        "start": start.astimezone(PACIFIC),
        "end": end.astimezone(PACIFIC),
        "gym_variant": gym_variant,
        "source_event_url": url,
    }


# ── stage 3: court count rule ─────────────────────────────────────────────


def _court_count(venue: str, gym_variant: str | None) -> int:
    """Apply the venue-specific rule:
    - The Armory: always 1 court (regardless of full/half wording).
    - Red Morton: 1 court for Half Gym, 2 courts for Full Gym.
    - Unknown variant on RMCC: assume 1 court (safer than over-claiming).
    """
    if ARMORY_VENUE_HINT in (venue or "").lower():
        return 1
    if (gym_variant or "").lower() == "full":
        return 2
    return 1


# ── stage 4: time + interval helpers ─────────────────────────────────────


def _format_pretty(dt: datetime) -> str:
    """'14:30' → '2:30 PM'."""
    h = dt.hour
    suffix = "AM" if h < 12 else "PM"
    h12 = h % 12 or 12
    return f"{h12}:{dt.minute:02d} {suffix}"


def _format_range_pretty(start: datetime, end: datetime) -> str:
    return f"{_format_pretty(start)}–{_format_pretty(end)}"


def _merge_intervals(
    intervals: list[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
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


# ── stage 5: bucket → merged Sessions with court breakdown ───────────────


def _build_sessions(events: list[dict]) -> list[Session]:
    """Group raw events by (venue, court_count, address, source_event_url
    excluded), merge their time intervals, and emit Sessions whose `courts`
    field describes which physical courts are open during each merged window.
    """
    # Group by the discriminating fields. We do NOT group by
    # source_event_url because every original event has its own URL — we
    # arbitrarily keep the first one when multiple events merge.
    buckets: dict[tuple[str, int], list[dict]] = {}
    for ev in events:
        venue = ev["venue"]
        n_courts = _court_count(venue, ev["gym_variant"])
        buckets.setdefault((venue, n_courts), []).append(ev)

    out: list[Session] = []
    for (venue, n_courts), group in buckets.items():
        intervals = [(ev["start"], ev["end"]) for ev in group]
        merged = _merge_intervals(intervals)

        # Pick a representative address + first source URL per bucket.
        address = next((ev["address"] for ev in group if ev.get("address")), None)
        first_url = group[0]["source_event_url"]

        is_armory = ARMORY_VENUE_HINT in venue.lower()
        # The schema-level merge_adjacent_sessions in schema.py groups by
        # (venue, activity_key). If half-gym and full-gym sessions back at
        # the same venue ever sit back-to-back, we DON'T want them to merge
        # — they really do represent different physical court availability.
        # Bake the court config into the activity so the keys differ.
        if is_armory:
            activity = "Drop-In Basketball"  # Armory only has one court anyway
        elif n_courts == 2:
            activity = "Drop-In Basketball (Full Gym)"
        else:
            activity = "Drop-In Basketball (Half Gym)"

        for interval_start, interval_end in merged:
            window_pretty = _format_range_pretty(interval_start, interval_end)

            courts = [
                {"name": f"Court {i + 1}", "windows": [window_pretty]}
                for i in range(n_courts)
            ]

            if n_courts == 2:
                notes = (
                    f"Full gym: both Court 1 and Court 2 are open. "
                    f"{window_pretty}. Drop-in; subject to change for rentals "
                    f"or programs."
                )
            elif is_armory:
                notes = (
                    f"The single Armory court is open. {window_pretty}. "
                    f"Drop-in; subject to change for rentals or programs."
                )
            else:
                notes = (
                    f"Half gym: only Court 1 is open. {window_pretty}. "
                    f"Drop-in; subject to change for rentals or programs."
                )

            out.append(
                Session(
                    venue=venue,
                    start=interval_start.isoformat(timespec="minutes"),
                    end=interval_end.isoformat(timespec="minutes"),
                    activity=activity,
                    address=address,
                    cost="$5 adult / $1 youth/teen/senior",
                    notes=notes,
                    source_event_url=first_url,
                    courts=courts,
                )
            )

    out.sort(key=lambda s: s.start)
    return out


# ── orchestration ─────────────────────────────────────────────────────────


def scrape() -> ScrapeResult:
    result = ScrapeResult(source=SOURCE_NAME, source_url=SOURCE_URL)
    window_start, window_end = data_window_range()
    print(
        f"[{SOURCE_NAME}] window {window_start.date()} – {window_end.date()}",
        file=sys.stderr,
    )

    raw_events: list[dict] = []
    with sync_playwright() as pw:
        # Akamai blocks bundled Chromium. Use the locally installed Chrome.
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
        urls = collect_basketball_event_urls(page)
        print(f"[{SOURCE_NAME}] found {len(urls)} basketball events", file=sys.stderr)
        for url in urls:
            try:
                event = parse_event_page(page, url)
            except Exception as e:  # noqa: BLE001
                print(f"[{SOURCE_NAME}] failed {url}: {e}", file=sys.stderr)
                continue
            if event:
                raw_events.append(event)
        browser.close()

    print(
        f"[{SOURCE_NAME}] {len(raw_events)} raw events in data window",
        file=sys.stderr,
    )

    sessions = _build_sessions(raw_events)
    for s in sessions:
        try:
            result.add_session(s)
        except Exception as e:  # noqa: BLE001
            print(f"[{SOURCE_NAME}] invalid session: {e}", file=sys.stderr)

    result.sessions.sort(key=lambda s: s.start)
    print(
        f"[{SOURCE_NAME}] kept {len(result.sessions)} merged sessions",
        file=sys.stderr,
    )
    return result


if __name__ == "__main__":
    out = write_result(scrape())
    print(f"wrote {out}")
