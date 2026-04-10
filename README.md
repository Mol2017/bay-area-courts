# South Bay Area Drop-In Basketball

Scrapes indoor drop-in basketball court schedules from multiple south bay
recreation centers and shows them in a single weekly calendar. View the
current week and next week's availability across all venues at a glance.

**Live site:** https://mol2017.github.io/bay-area-courts

## Architecture

```
┌────────────────────────────────────────────────────────────────┐
│                        Source Websites                         │
└──────┬──────────────┬──────────────┬──────────────┬────────────┘
       │              │              │              │
       ▼              ▼              ▼              ▼
┌────────────┐ ┌────────────┐ ┌────────────┐ ┌────────────┐
│  Scraper   │ │  Scraper   │ │  Scraper   │ │  Scraper   │
│  RMCC      │ │  PCC       │ │  AFG       │ │  SC        │
│  Redwood   │ │  Redwood   │ │  Menlo Pk  │ │  Newark    │
└─────┬──────┘ └─────┬──────┘ └─────┬──────┘ └─────┬──────┘
      │              │              │              │
      ▼              ▼              ▼              ▼
┌────────────────────────────────────────────────────────────────┐
│  Schema (schema.py)                                            │
│  Shared Session format · validates · merges adjacent sessions  │
└──────┬──────────────┬──────────────┬──────────────┬────────────┘
       │              │              │              │
       ▼              ▼              ▼              ▼
   raw/rmcc.json  raw/pcc.json  raw/afg.json   raw/sc.json
       │              │              │              │
       └──────────────┴──────┬───────┴──────────────┘
                             │
                             ▼
              ┌───────────────────────────┐
              │  Merge (merge.py)         │
              │  Combines all raw/*.json  │
              │  Filters to 2-week window │
              └────────────┬──────────────┘
                           │
                           ▼
                    data/merged.json
                           │
                           ▼
              ┌───────────────────────────┐
              │  Frontend (web/)          │
              │  Weekly calendar UI       │
              │  Loads merged.json        │
              └───────────────────────────┘
```

## Sources

| Short | Venue | City | Cost | Source |
|---|---|---|---|---|
| **AFG** | Arrillaga Family Gymnasium | Menlo Park | Free drop-in | CatchCorner rental listings |
| **RMCC** | Red Morton Community Center | Redwood City | $5 adult / $1 youth | City calendar + JSON-LD |
| **SC** | Silliman Center | Newark | $14/person | Facebook poster via RSS + OCR |
| **PCC** | Peninsula Community Center | Redwood City | $55/person | Mindbody schedule |

## Install

```bash
git clone https://github.com/Mol2017/bay-area-courts.git
cd bay-area-courts
pip install -r requirements.txt
python -m playwright install --with-deps chromium
```

## Run locally

```bash
# Scrape all sources + merge
python scripts/run_all_scrapers.py
python scripts/merge.py

# Serve the calendar (with working Refresh button)
python scripts/serve.py
# → http://127.0.0.1:8765/web/
```

## Deploy

See [DEPLOY.md](DEPLOY.md) for GitHub Pages + Actions deployment with
weekly cron and a working in-page Refresh button.

## Add a new venue

1. Add `scrapers/<venue_name>.py` with a `scrape()` function that returns
   a `ScrapeResult` (see `scrapers/schema.py` for the schema).
2. Add a color + short name in `web/app.js` under `SOURCE_COLORS` and
   `SOURCE_INFO`.
3. Push — `run_all_scrapers.py` auto-discovers new `scrapers/*.py` files.
