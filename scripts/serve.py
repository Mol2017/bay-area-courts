"""Local dev server: static files + a /api/refresh endpoint.

Usage:
    python3 scripts/serve.py [--port 8765] [--host 127.0.0.1]

This is a thin wrapper around stdlib `http.server` that also handles:

    POST /api/refresh
        1. Deletes every data/raw/*.json and data/merged.json.
        2. Runs scripts/run_all_scrapers.py.
        3. Runs scripts/merge.py.
        4. Returns {"status": "ok", "sessions": N, "duration_s": ...}.

A single global lock guards concurrent refreshes — clicking refresh twice
returns 409 Conflict on the second click.

Note: this only works when the site is served by *this* script. On GitHub
Pages there's no backend, so the refresh button there is a no-op (the data
file still updates via the weekly cron in .github/workflows/refresh.yml).
"""
from __future__ import annotations

import argparse
import http.server
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RAW = REPO / "data" / "raw"
MERGED = REPO / "data" / "merged.json"

_refresh_lock = threading.Lock()


def run_refresh() -> dict:
    """Clear raw, scrape, merge. Returns a result dict (raises on failure)."""
    started = time.monotonic()

    # 1) Wipe stale data so a failed scraper can't leave half-old, half-new state.
    if RAW.exists():
        for f in RAW.glob("*.json"):
            f.unlink()
    if MERGED.exists():
        MERGED.unlink()

    # 2) Scrape.
    scrape = subprocess.run(
        [sys.executable, "scripts/run_all_scrapers.py"],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    if scrape.returncode != 0:
        raise RuntimeError(
            f"scrape failed (rc={scrape.returncode}): {scrape.stderr[-1500:]}"
        )

    # 3) Merge.
    merge = subprocess.run(
        [sys.executable, "scripts/merge.py"],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    if merge.returncode != 0:
        raise RuntimeError(
            f"merge failed (rc={merge.returncode}): {merge.stderr[-1500:]}"
        )

    if not MERGED.exists():
        raise RuntimeError("merge ran but produced no merged.json")

    data = json.loads(MERGED.read_text())
    return {
        "status": "ok",
        "sessions": len(data.get("sessions", [])),
        "sources": sorted({s.get("source") for s in data.get("sessions", []) if s.get("source")}),
        "duration_s": round(time.monotonic() - started, 1),
        "scrape_log": scrape.stdout[-1500:],
        "merge_log": merge.stdout[-500:],
    }


class Handler(http.server.SimpleHTTPRequestHandler):
    # Serve from the repo root so both /web/* and /data/* are reachable.
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(REPO), **kwargs)

    # Always disable caching so the page always sees the latest merged.json.
    def end_headers(self):
        self.send_header("Cache-Control", "no-store, must-revalidate")
        super().end_headers()

    def do_POST(self):  # noqa: N802 (stdlib naming)
        if self.path == "/api/refresh":
            self.handle_refresh()
        else:
            self.send_error(404, "no POST handler for this path")

    def handle_refresh(self):
        acquired = _refresh_lock.acquire(blocking=False)
        if not acquired:
            self._send_json(
                409,
                {"status": "busy", "message": "another refresh is already running"},
            )
            return
        try:
            print(f"[serve] refresh started by {self.client_address[0]}", flush=True)
            try:
                result = run_refresh()
            except Exception as e:  # noqa: BLE001
                print(f"[serve] refresh failed: {e}", flush=True)
                self._send_json(500, {"status": "error", "message": str(e)})
                return
            print(
                f"[serve] refresh ok: {result['sessions']} sessions in "
                f"{result['duration_s']}s",
                flush=True,
            )
            self._send_json(200, result)
        finally:
            _refresh_lock.release()

    def _send_json(self, code: int, body: dict) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    # Quieter logs.
    def log_message(self, format: str, *args) -> None:  # noqa: A002
        sys.stderr.write(
            "[%s] %s\n" % (self.log_date_time_string(), format % args)
        )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()

    # ThreadingHTTPServer so a slow refresh request doesn't block static file
    # serving for the same browser tab.
    with http.server.ThreadingHTTPServer((args.host, args.port), Handler) as httpd:
        url = f"http://{args.host}:{args.port}/web/"
        print(f"serving on {url}", flush=True)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nshutting down", flush=True)
            return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
