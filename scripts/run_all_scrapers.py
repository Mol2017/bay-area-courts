"""Run every scraper module in ``scrapers/`` and write its JSON to ``data/raw/``.

Discovers scrapers by globbing ``scrapers/*.py``. Each scraper module is
expected to expose a top-level ``scrape() -> ScrapeResult`` function.
Modules without a ``scrape()`` (e.g. ``base.py``, ``schema.py``) are
skipped.

Exit code is non-zero if any scraper raised. The merge step in
``scripts/merge.py`` is intentionally a separate command, so a partial
failure here doesn't poison ``data/merged.json``.

Defensive write: if a scraper returns a ``ScrapeResult`` with **zero**
sessions but ``data/raw/<source>.json`` already exists with sessions in
it, the existing file is preserved instead of being overwritten with an
empty one. This protects against environments where a scraper silently
fails (e.g. Facebook serving a generic placeholder image to GitHub
Actions IPs for the Newark scraper) and would otherwise wipe out the
last-good data.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRAPERS_DIR = REPO_ROOT / "scrapers"
RAW_DIR = REPO_ROOT / "data" / "raw"

# Make scrapers/ importable so each module can `from base import ...`.
sys.path.insert(0, str(SCRAPERS_DIR))

# Modules in scrapers/ that don't expose a scrape() function and should be
# silently skipped instead of logged as "skipped".
_NON_SCRAPER_FILES = {"base.py", "schema.py", "__init__.py"}


def _load_module(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _existing_session_count(source: str) -> int:
    path = RAW_DIR / f"{source}.json"
    if not path.exists():
        return 0
    try:
        doc = json.loads(path.read_text())
    except json.JSONDecodeError:
        return 0
    return len(doc.get("sessions") or [])


def main() -> int:
    failures = 0
    for path in sorted(SCRAPERS_DIR.glob("*.py")):
        if path.name in _NON_SCRAPER_FILES:
            continue
        print(f"==> {path.name}")
        try:
            mod = _load_module(path)
            if not hasattr(mod, "scrape"):
                print(f"  skipped: no scrape() in {path.name}")
                continue
            result = mod.scrape()

            # Defensive write — see module docstring.
            if not result.sessions:
                existing = _existing_session_count(result.source)
                if existing > 0:
                    print(
                        f"  preserved existing {result.source}.json "
                        f"({existing} sessions) — scrape returned 0"
                    )
                    continue

            from base import write_result  # local import after sys.path tweak

            out = write_result(result)
            print(f"  wrote {out.relative_to(REPO_ROOT)}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"  FAILED: {e}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
