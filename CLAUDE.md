# CLAUDE.md — Project context for Claude Code

## What this is

A static calendar website that shows indoor drop-in basketball court
availability in the south bay area. Data is scraped from 4 recreation
center websites, merged into a single JSON, and rendered as a weekly
calendar hosted on GitHub Pages.

Live: https://mol2017.github.io/bay-area-courts

## Repo layout

```
scrapers/
  schema.py                    # Session/ScrapeResult dataclasses, merge logic, write_result()
  base.py                     # Shared helpers: data_window_range, in_data_window, parse_iso, polite_goto
  red_morton_community_center.py   # RMCC — Playwright + JSON-LD, half/full gym → court breakdown
  peninsula_community_center.py    # PCC  — Playwright + Mindbody DOM, week 1 + week 2 nav
  arrillaga_family_gymnasium.py    # AFG  — Playwright + CatchCorner tiles, scroll lazy-load
  newark_community_center.py       # SC   — RSS feed (rss.app) + EasyOCR on FB poster image

scripts/
  run_all_scrapers.py          # Globs scrapers/*.py, calls scrape(), defensive write
  merge.py                     # Reads data/raw/*.json → data/merged.json (2-week window)
  serve.py                     # Local dev server: static files + POST /api/refresh

web/
  index.html                   # Calendar page
  app.js                       # All rendering, week nav, popup, source filter, refresh logic
  style.css                    # Layout + popup styles
  config.js                    # Optional: repoOwner/repoName override for GH API

data/
  raw/<source>.json            # One per scraper, committed to git
  raw/images/                  # Newark poster JPEGs (committed)
  merged.json                  # Union of all raw files, what the frontend reads

.github/workflows/refresh.yml # Weekly cron + workflow_dispatch
```

## Key conventions

- **All times are Pacific** (`America/Los_Angeles`). Python uses `zoneinfo.ZoneInfo("America/Los_Angeles")`, JS uses `Intl.DateTimeFormat` with `timeZone: "America/Los_Angeles"`.
- **Data window is 2 weeks** (current Monday → Monday after next). Controlled by `WEEKS_AHEAD = 1` in `scrapers/base.py` and mirrored in `scripts/merge.py`.
- **ISO-8601 with TZ offset** for all start/end times in JSON (e.g. `2026-04-06T10:00-07:00`).
- **Adjacent-session merging** happens automatically in `schema.write_result()`. Sessions with matching `(venue, activity_key)` that touch or overlap get collapsed.
- **Defensive write**: `run_all_scrapers.py` won't overwrite a non-empty raw file with an empty result. Protects against scraper failures.
- **`run_all_scrapers.py` always exits 0** so the GH Actions merge+commit steps run even when one scraper fails.

## Scraper interface

Each `scrapers/<name>.py` must expose:
```python
def scrape() -> ScrapeResult:
    ...
```

Use `from base import ScrapeResult, Session, data_window_range, in_data_window, polite_goto, write_result`.

Multi-court venues (RMCC, AFG, SC) compute a per-day union of Court 1 + Court 2 windows and populate `Session.courts` (a list of `{"name": "Court 1", "windows": ["6:00 AM–5:00 PM"]}` dicts) for the popup.

## Frontend

- `SOURCE_COLORS` — pastel color per source slug.
- `SOURCE_INFO` — short name, city, drop-in price per source. Controls legend sort (cheapest first) and event tile labels.
- `DEFAULT_OFF` — sources unchecked by default (currently PCC at $55).
- Refresh button: tries `POST /api/refresh` (local), falls back to GitHub Actions `workflow_dispatch` via PAT stored in `localStorage`.

## Newark scraper (special)

Uses an RSS mirror (`rss.app/feeds/4ob5vMivToevVCvG.xml`) of the Newark Rec Facebook page instead of scraping FB directly. No Playwright needed. The RSS provides signed FB CDN image URLs. EasyOCR reads the poster image and reconstructs the schedule table geometrically (find day rows by Y-coordinate, court columns by X, pair time tokens sequentially).

## Hard-coded venue knowledge

| Source | Cost | Notes |
|---|---|---|
| AFG | Free drop-in (or $88/hr to reserve) | CatchCorner lists as rental; free if unreserved |
| RMCC | $5 adult / $1 youth/teen/senior | From the RWC website |
| SC | $14/person | From venue staff, not on the FB poster |
| PCC | $55/person | From venue staff, not on Mindbody |

## How to run

```bash
pip install -r requirements.txt          # playwright, easyocr
python -m playwright install --with-deps chromium
python scripts/run_all_scrapers.py       # scrape all 4 sources
python scripts/merge.py                  # → data/merged.json
python scripts/serve.py                  # → http://127.0.0.1:8765/web/
```
