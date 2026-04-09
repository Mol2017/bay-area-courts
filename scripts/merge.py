"""Merge ``data/raw/*.json`` into a single ``data/merged.json``.

Reads every per-source raw file produced by ``run_all_scrapers.py``,
flattens their session lists, sorts by start time, and writes one combined
file the frontend can fetch.

Output schema (consumed by ``web/app.js``)::

    {
      "generated_at": "2026-04-09T15:30:00-07:00",
      "window_start": "2026-04-06",   # this Monday in PT
      "window_end":   "2026-04-20",   # Monday after WEEKS_AHEAD more weeks
      "weeks":        2,              # window_end - window_start, in weeks
      "sessions": [ ... ]
    }

Sessions outside ``[window_start, window_end)`` are dropped — this is a
defensive backstop; the scrapers themselves already filter on the same
window via ``scrapers/base.in_data_window``. Keep ``WEEKS_AHEAD`` here in
sync with ``scrapers/base.py``.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = REPO_ROOT / "data" / "raw"
OUT = REPO_ROOT / "data" / "merged.json"
PACIFIC = ZoneInfo("America/Los_Angeles")

# Must match scrapers/base.py: WEEKS_AHEAD = 1 → 2-week window.
WEEKS_AHEAD = 1


def data_window(now: datetime | None = None) -> tuple[datetime, datetime]:
    """Return (this Monday 00:00, Monday after `WEEKS_AHEAD` weeks 00:00) PT."""
    now = (now or datetime.now(PACIFIC)).astimezone(PACIFIC)
    monday = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return monday, monday + timedelta(days=7 * (1 + WEEKS_AHEAD))


def main() -> int:
    if not RAW_DIR.exists():
        print(f"no raw dir at {RAW_DIR}", file=sys.stderr)
        return 1

    window_start, window_end = data_window()
    all_sessions: list[dict] = []

    for path in sorted(RAW_DIR.glob("*.json")):
        try:
            doc = json.loads(path.read_text())
        except json.JSONDecodeError as e:
            print(f"skip {path.name}: {e}", file=sys.stderr)
            continue
        source = doc.get("source") or path.stem
        for s in doc.get("sessions", []):
            s = {**s, "source": source}
            try:
                start = datetime.fromisoformat(s["start"]).astimezone(PACIFIC)
            except (KeyError, ValueError):
                continue
            if not (window_start <= start < window_end):
                continue
            all_sessions.append(s)

    all_sessions.sort(key=lambda s: s["start"])

    payload = {
        "generated_at": datetime.now(PACIFIC).isoformat(timespec="seconds"),
        "window_start": window_start.date().isoformat(),
        "window_end": window_end.date().isoformat(),
        "weeks": WEEKS_AHEAD + 1,
        "sessions": all_sessions,
    }
    OUT.write_text(json.dumps(payload, indent=2))
    print(f"wrote {OUT.relative_to(REPO_ROOT)} ({len(all_sessions)} sessions)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
