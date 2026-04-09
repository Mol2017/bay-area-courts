"""Run every scraper module in ``scrapers/`` and write its JSON to ``data/raw/``.

Discovers scrapers by globbing ``scrapers/*.py``. Each scraper module is
expected to expose a top-level ``scrape() -> ScrapeResult`` function.
Modules without a ``scrape()`` (e.g. ``base.py``, ``schema.py``) are
skipped.

Exit code is non-zero if any scraper raised. The merge step in
``scripts/merge.py`` is intentionally a separate command, so a partial
failure here doesn't poison ``data/merged.json``.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRAPERS_DIR = REPO_ROOT / "scrapers"

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
            from base import write_result  # local import after sys.path tweak

            out = write_result(result)
            print(f"  wrote {out.relative_to(REPO_ROOT)}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"  FAILED: {e}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
