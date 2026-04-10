"""Scraper for Silliman Center (Newark Community Center) court schedule.

Source: https://www.facebook.com/Newarkrec/

Newark Recreation publishes the Silliman Center weekly court availability as
an *image* in a Facebook post. The pipeline:

1. Find the Silliman post on the Newark Rec FB page (public, no login).
2. Get the high-resolution image URL from the photo permalink.
3. Download the image to data/raw/images/.
4. Run EasyOCR locally and reconstruct the schedule table by finding day
   rows + court columns geometrically and pairing time tokens.
5. Emit Sessions for (day, court, window) entries inside the 2-week data
   window. Older posters' dates fall outside the window and are dropped.

The schedule poster is fetched via an RSS mirror of the Facebook page
(rss.app) so the scraper needs no Playwright, no login, no proxy, and
works identically on residential IPs and GitHub Actions runners.

Pipeline:
1. Fetch the RSS XML (plain HTTP, ~10 KB).
2. Find items whose title matches "Court Availability" or "Court Schedule".
3. Download the poster image from the signed FB CDN URL embedded in each
   item (the CDN accepts these signed URLs from any IP).
4. Run EasyOCR to reconstruct the court schedule table.
5. Compute the per-day Court 1 + Court 2 union and emit Sessions.

Newark Rec only ever publishes past or current weeks — there's no advance
schedule — so this scraper only ever contributes current-week sessions in
practice. The other three scrapers fill in the next-week column.

Dependencies:
- easyocr  (for OCR of the poster image)
"""
from __future__ import annotations

import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path

from base import (
    PACIFIC,
    REPO_ROOT,
    ScrapeResult,
    Session,
    data_window_range,
    write_result,
)

SOURCE_NAME = "newark_community_center"
SOURCE_URL = "https://www.facebook.com/Newarkrec/"
RSS_URL = "https://rss.app/feeds/4ob5vMivToevVCvG.xml"
VENUE = "Silliman Center"
ADDRESS = "6800 Mowry Ave, Newark, CA 94560"

IMAGES_DIR = REPO_ROOT / "data" / "raw" / "images"

# ── stage 1: fetch Silliman poster images from RSS ────────────────────────

COURT_TITLE_RE = re.compile(
    r"Court\s+Availab|Court\s+Sched", re.IGNORECASE
)


def _fetch_rss_items() -> list[dict]:
    """Fetch the RSS mirror of the Newark Rec FB page and return items
    whose title mentions "Court Availability" or "Court Schedule".

    Each returned dict has keys: ``title``, ``image_url``, ``pub_date``.
    The ``image_url`` is the signed FB CDN URL embedded in the ``<description>``
    or ``<media:content>`` element — it works from any IP because the CDN
    honours the signature, not the caller's IP.
    """
    print(f"[{SOURCE_NAME}] fetching RSS feed…", file=sys.stderr)
    req = urllib.request.Request(
        RSS_URL,
        headers={"User-Agent": "bay-area-courts/1.0"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        xml_bytes = r.read()

    root = ET.fromstring(xml_bytes)
    out: list[dict] = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        if not COURT_TITLE_RE.search(title):
            continue
        # Image URL: prefer <description> img src (has the signed CDN URL),
        # fall back to <media:content> or <enclosure>.
        desc = item.findtext("description") or ""
        img_match = re.search(r'<img[^>]+src="([^"]+)"', desc)
        image_url = img_match.group(1) if img_match else None
        if not image_url:
            # Try media:content
            for ns in (
                "{http://search.yahoo.com/mrss/}",
                "{http://purl.org/rss/1.0/modules/content/}",
            ):
                mc = item.find(f"{ns}content")
                if mc is not None and mc.get("url"):
                    image_url = mc.get("url")
                    break
        if not image_url:
            enc = item.find("enclosure")
            if enc is not None:
                image_url = enc.get("url")
        pub_date = (item.findtext("pubDate") or "").strip()
        out.append({"title": title, "image_url": image_url, "pub_date": pub_date})

    print(f"[{SOURCE_NAME}] RSS: {len(out)} court-schedule items", file=sys.stderr)
    return out


def _download_image(url: str, label: str) -> Path:
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    out = IMAGES_DIR / f"newark_silliman_{label}.jpg"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            "Referer": "https://www.facebook.com/",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        out.write_bytes(resp.read())
    return out


# ── stage 2: caption date-range parsing (best-effort fallback) ────────────


_CAPTION_RE = re.compile(
    r"Silliman Center Weekly Court Availability for ([A-Za-z]+)\s+(\d{1,2})"
    r"\s*[–\-]\s*(?:([A-Za-z]+)\s+)?(\d{1,2})",
    re.IGNORECASE,
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
_MONTHS_ABBR = {
    name[:3]: idx for name, idx in _MONTHS.items()
}


def parse_caption_range(caption: str) -> tuple[datetime, datetime] | None:
    m = _CAPTION_RE.search(caption or "")
    if not m:
        return None
    start_month_name = m.group(1)
    start_day = int(m.group(2))
    end_month_name = m.group(3) or start_month_name
    end_day = int(m.group(4))
    start_month = _MONTHS.get(start_month_name.title())
    end_month = _MONTHS.get(end_month_name.title())
    if not start_month or not end_month:
        return None

    today = datetime.now(PACIFIC)
    year = today.year
    try:
        start = datetime(year, start_month, start_day, tzinfo=PACIFIC)
    except ValueError:
        return None
    if abs((start - today).days) > 200:
        year = year - 1 if start > today else year + 1
        start = datetime(year, start_month, start_day, tzinfo=PACIFIC)
    end_year = year if end_month >= start_month else year + 1
    try:
        end = datetime(end_year, end_month, end_day, tzinfo=PACIFIC)
    except ValueError:
        return None
    return start, end


# ── stage 3: EasyOCR + table reconstruction ───────────────────────────────

# Lazy-loaded so importing this module is cheap and run_all_scrapers doesn't
# pay the EasyOCR init cost when other scrapers run.
_reader = None


def _get_reader():
    global _reader
    if _reader is None:
        import easyocr  # heavy import — keep lazy

        _reader = easyocr.Reader(["en"], gpu=False, verbose=False)
    return _reader


_DAY_RE = re.compile(
    r"\b(MON|TUE|WED|THU|FRI|SAT|SUN)[A-Z]*\s+(\d{1,2})\s*/\s*(\d{1,2})\b",
    re.IGNORECASE,
)
# Lenient time matcher: accepts H:MM, H.MM, or HMM (no separator), with AM/PM.
_TIME_RE = re.compile(r"(\d{1,2})[:.]?(\d{2})\s*([AP])\.?M", re.IGNORECASE)


def _normalize_time_token(s: str) -> str:
    """Tidy common OCR confusions in time tokens."""
    s = s.replace("*", ":")
    s = re.sub(r"\.(\d{2})", r":\1", s)               # 6.00 -> 6:00
    s = re.sub(r"[Oo]{1,2}(?=\s*[AP]M)", "00", s)     # 7:OO PM -> 7:00 PM
    s = re.sub(r"(?<=:)[Oo]", "0", s)                 # :O0 -> :00
    return s


def _ocr_boxes(image_path: Path) -> list[dict]:
    reader = _get_reader()
    raw = reader.readtext(str(image_path))
    out = []
    for box, text, conf in raw:
        out.append(
            {
                "cx": sum(p[0] for p in box) / 4,
                "cy": sum(p[1] for p in box) / 4,
                "text": text,
                "norm": _normalize_time_token(text),
                "conf": float(conf),
            }
        )
    return out


def _cluster_visual_rows(boxes: list[dict], y_tol: float = 20) -> list[list[dict]]:
    """Group boxes that share a horizontal band, then sort each band by x."""
    boxes = sorted(boxes, key=lambda b: b["cy"])
    rows: list[list[dict]] = []
    for b in boxes:
        if rows and abs(b["cy"] - rows[-1][-1]["cy"]) <= y_tol:
            rows[-1].append(b)
        else:
            rows.append([b])
    for r in rows:
        r.sort(key=lambda b: b["cx"])
    return rows


def reconstruct_schedule(image_path: Path, year: int) -> list[dict]:
    """Run OCR + table reconstruction. Returns list of:
       {date: 'YYYY-MM-DD', court: 'Court 1'|'Court 2', windows: [(start, end)...]}
    where start/end are 'HH:MM' strings (24-hour).
    """
    boxes = _ocr_boxes(image_path)

    # Day labels: anchor for rows.
    day_anchors = []
    for b in boxes:
        m = _DAY_RE.search(b["text"])
        if not m:
            continue
        day_anchors.append(
            {
                "day": m.group(1).upper()[:3],
                "month": int(m.group(2)),
                "dom": int(m.group(3)),
                "y": b["cy"],
            }
        )
    if len(day_anchors) < 2:
        return []
    day_anchors.sort(key=lambda r: r["y"])

    # Court column anchors.
    court_x = {}
    for b in boxes:
        u = b["text"].upper()
        if "COURT 1" in u:
            court_x[1] = b["cx"]
        elif "COURT 2" in u:
            court_x[2] = b["cx"]
    if 1 not in court_x or 2 not in court_x:
        return []

    date_max = court_x[1] / 2 + 50
    court1_max = (court_x[1] + court_x[2]) / 2

    def assign_court(cx: float) -> int | None:
        if cx < date_max:
            return None
        if cx < court1_max:
            return 1
        return 2

    row_spacing = (day_anchors[-1]["y"] - day_anchors[0]["y"]) / max(
        1, len(day_anchors) - 1
    )
    half_band = row_spacing / 2

    cells: dict[tuple[str, int], list[dict]] = {}
    for b in boxes:
        if _DAY_RE.search(b["text"]):
            continue
        upper = b["text"].upper()
        if any(
            kw in upper
            for kw in (
                "COURT",
                "DATE",
                "AVAILABILITY",
                "NEWARK",
                "APRIL",
                "MARCH",
                "MAY",
                "JUNE",
                "JULY",
                "AUGUST",
                "SEPTEMBER",
                "OCTOBER",
                "NOVEMBER",
                "DECEMBER",
                "JANUARY",
                "FEBRUARY",
                "SUBJECT",
                "CHANGE",
                "WEEKLY",
                "SILLIMAN",
                "GILLIMAN",
            )
        ):
            continue
        court = assign_court(b["cx"])
        if not court:
            continue
        nearest = min(day_anchors, key=lambda r: abs(r["y"] - b["cy"]))
        if abs(nearest["y"] - b["cy"]) > half_band + 10:
            continue
        cells.setdefault((nearest["day"], court), []).append(b)

    schedule: list[dict] = []
    for (day, court), cell_boxes in sorted(cells.items()):
        anchor = next(a for a in day_anchors if a["day"] == day)
        # Combine cell text in true reading order.
        visual = _cluster_visual_rows(cell_boxes, y_tol=20)
        ordered = [b for row in visual for b in row]
        combined = " ".join(b["norm"] for b in ordered)

        tokens: list[tuple[int, int, str]] = []
        for m in _TIME_RE.finditer(combined):
            hour = int(m.group(1))
            minute = int(m.group(2))
            ap = m.group(3).upper()
            if hour == 12:
                hour = 0  # 12:xx AM/PM normalization
            if ap == "P":
                hour += 12
            if 0 <= hour < 24 and 0 <= minute < 60:
                tokens.append((hour, minute, ap))

        if len(tokens) < 2:
            continue

        windows = []
        for i in range(0, len(tokens) - 1, 2):
            sh, sm, _ = tokens[i]
            eh, em, _ = tokens[i + 1]
            windows.append((f"{sh:02d}:{sm:02d}", f"{eh:02d}:{em:02d}"))

        try:
            date = datetime(year, anchor["month"], anchor["dom"], tzinfo=PACIFIC)
        except ValueError:
            continue
        schedule.append(
            {
                "date": date.date().isoformat(),
                "court": f"Court {court}",
                "windows": windows,
            }
        )

    return schedule


# ── stage 4: per-day Court 1 + Court 2 union ──────────────────────────────


def _to_minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _from_minutes(total: int) -> str:
    return f"{total // 60:02d}:{total % 60:02d}"


def _format_pretty(hhmm: str) -> str:
    """'06:00' → '6:00 AM', '17:30' → '5:30 PM'."""
    h, m = (int(p) for p in hhmm.split(":"))
    suffix = "AM" if h < 12 else "PM"
    h12 = h % 12 or 12
    return f"{h12}:{m:02d} {suffix}"


def _format_range_pretty(start: str, end: str) -> str:
    return f"{_format_pretty(start)}–{_format_pretty(end)}"


def _merge_intervals(
    intervals: list[tuple[int, int]],
) -> list[tuple[int, int]]:
    """Sort + collapse overlapping/touching minute-range intervals."""
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
    per_court: dict[int, list[tuple[str, str]]],
) -> list[dict]:
    """Structured per-court breakdown for the Session.courts field.

    Example: [{"name": "Court 1", "windows": ["6:00 AM–5:00 PM", "6:00 PM–9:00 PM"]},
              {"name": "Court 2", "windows": ["6:00 AM–3:30 PM"]}]
    """
    out: list[dict] = []
    for court_num in sorted(per_court.keys()):
        ranges = sorted(per_court[court_num])
        merged_min = _merge_intervals(
            [(_to_minutes(s), _to_minutes(e)) for s, e in ranges]
        )
        pretty = [
            _format_range_pretty(_from_minutes(s), _from_minutes(e))
            for s, e in merged_min
        ]
        out.append({"name": f"Court {court_num}", "windows": pretty})
    return out


def _build_court_breakdown(per_court: dict[int, list[tuple[str, str]]]) -> str:
    """Same data as the structured form but flattened to a notes string."""
    parts: list[str] = []
    for entry in _build_court_breakdown_struct(per_court):
        parts.append(f"{entry['name']}: " + ", ".join(entry["windows"]))
    return ". ".join(parts) + "."


def _build_day_sessions(
    date_iso: str, per_court: dict[int, list[tuple[str, str]]]
) -> list[Session]:
    """Compute the union of Court 1 + Court 2 windows for a single day and
    emit one Session per merged window.
    """
    if not per_court:
        return []

    # 1. Union: combine all (start, end) from every court, merge overlaps.
    all_intervals: list[tuple[int, int]] = []
    for windows in per_court.values():
        for s, e in windows:
            all_intervals.append((_to_minutes(s), _to_minutes(e)))
    union = _merge_intervals(all_intervals)

    # 2. Per-court breakdown — both structured (for the popup) and flattened
    #    text (for the notes string), so the same data is available either way.
    courts_struct = _build_court_breakdown_struct(per_court)
    breakdown = _build_court_breakdown(per_court)
    courts_present = sorted(per_court.keys())
    courts_label = (
        "Court 1 + Court 2"
        if len(courts_present) == 2
        else f"Court {courts_present[0]}"
    )
    notes = (
        f"Combined {courts_label} availability at Silliman Center. "
        f"{breakdown} "
        f"Scraped from weekly Newark Rec Facebook poster (EasyOCR)."
    )

    # 3. One Session per union window.
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
                activity="Drop-In Basketball",
                address=ADDRESS,
                # Hard-coded from venue staff, 2026-04 — not advertised on
                # the FB poster itself.
                cost="$14 per person (drop-in)",
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

    # Stage 1: fetch court-schedule items from the RSS mirror.
    rss_items = _fetch_rss_items()
    if not rss_items:
        print(f"[{SOURCE_NAME}] no court-schedule items in RSS", file=sys.stderr)
        return result

    # Stage 2: download each poster image and pair it with its caption.
    downloaded: list[tuple[Path, dict]] = []
    for idx, item in enumerate(rss_items):
        image_url = item.get("image_url")
        if not image_url:
            continue
        cap_range = parse_caption_range(item.get("title", ""))
        label_parts = [today.strftime("%Y-%m-%d"), f"post{idx + 1}"]
        if cap_range:
            label_parts.append(cap_range[0].strftime("%m-%d"))
        label = "_".join(label_parts)
        try:
            path = _download_image(image_url, label)
        except Exception as e:  # noqa: BLE001
            print(f"[{SOURCE_NAME}] download failed: {e}", file=sys.stderr)
            continue
        # Build a post dict compatible with the OCR stage below.
        post = {"captionText": item.get("title", ""), "feedSrc": image_url}
        downloaded.append((path, post))
        print(
            f"[{SOURCE_NAME}] downloaded {path.relative_to(REPO_ROOT)}",
            file=sys.stderr,
        )

    if not downloaded:
        return result

    # Collect every (date, court, window) from every OCR'd post into one
    # nested dict, deduping along the way. Older posts whose dates fall
    # outside the current week get dropped here.
    per_day: dict[str, dict[int, set[tuple[str, str]]]] = {}
    contributing: dict[str, int] = {p.name: 0 for p, _ in downloaded}

    for image_path, post in downloaded:
        cap_range = parse_caption_range(post.get("captionText", ""))
        year_for_ocr = cap_range[0].year if cap_range else today.year
        try:
            schedule = reconstruct_schedule(image_path, year_for_ocr)
        except Exception as e:  # noqa: BLE001
            print(
                f"[{SOURCE_NAME}] OCR failed on {image_path.name}: {e}",
                file=sys.stderr,
            )
            continue

        for entry in schedule:
            try:
                y, mo, d = (int(p) for p in entry["date"].split("-"))
                day_dt = datetime(y, mo, d, tzinfo=PACIFIC)
            except (KeyError, ValueError):
                continue
            if not (window_start <= day_dt < window_end):
                continue
            court_num = 1 if "1" in entry["court"] else 2
            for start_hhmm, end_hhmm in entry["windows"]:
                per_day.setdefault(entry["date"], {}).setdefault(
                    court_num, set()
                ).add((start_hhmm, end_hhmm))
                contributing[image_path.name] = (
                    contributing.get(image_path.name, 0) + 1
                )

    for fname, n in contributing.items():
        print(
            f"[{SOURCE_NAME}]   {fname}: {n} (date,court,window) entries in data window",
            file=sys.stderr,
        )

    # For each day, collapse Court 1 + Court 2 into one venue-level union and
    # create Sessions whose notes name the per-court breakdown.
    for date_iso in sorted(per_day.keys()):
        per_court_lists = {c: sorted(ws) for c, ws in per_day[date_iso].items()}
        for session in _build_day_sessions(date_iso, per_court_lists):
            try:
                result.add_session(session)
            except Exception as e:  # noqa: BLE001
                print(f"[{SOURCE_NAME}] invalid session: {e}", file=sys.stderr)

    result.sessions.sort(key=lambda s: s.start)
    print(
        f"[{SOURCE_NAME}] kept {len(result.sessions)} sessions in data window",
        file=sys.stderr,
    )
    return result


if __name__ == "__main__":
    out = write_result(scrape())
    print(f"wrote {out}")
