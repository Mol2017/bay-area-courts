# Deploy to GitHub Pages

Frontend on Pages, scrapers on GitHub Actions cron, Refresh button
triggers the workflow on demand via the GitHub REST API.

## Setup

### 1. Push to GitHub
```bash
gh repo create <you>/bay-area-courts --public --source=. --push
```

### 2. Enable Pages
Settings → Pages → Source: **Deploy from branch** → `main` / `/ (root)` → Save.

Site: `https://<you>.github.io/bay-area-courts`

### 3. Run the workflow once
Actions → "Refresh court data" → **Run workflow**. Takes ~4 minutes.

### 4. Set up the Refresh button (optional)
Create a fine-grained PAT at `github.com/settings/tokens?type=beta`:
- Repository: only this repo
- Permissions: **Actions → Read and write**

On the live site, click **Refresh** → paste the token. Stored in
`localStorage`, reused silently on future clicks.

## How it works

Weekly cron (Monday 7 AM PT) + manual `workflow_dispatch`:
```
checkout → pip install → playwright install → run scrapers → merge → commit + push
```

- Scrapers always exit 0 — a single failure doesn't block the others.
- Defensive write: if a scraper returns 0 sessions but the existing file
  has data, the existing file is preserved.
- Commit is a no-op when nothing changed.

The Refresh button tries `POST /api/refresh` (local dev path) first; on
Pages it gets 405, falls through to the GitHub Actions workflow dispatch,
polls the run, waits for Pages deploy, then reloads.

## Custom domain

Edit `web/config.js` if auto-detection fails:
```js
window.SITE_CONFIG = { repoOwner: "you", repoName: "bay-area-courts" };
```

## Local dev
```bash
pip install -r requirements.txt
python -m playwright install --with-deps chromium
python scripts/serve.py
# → http://127.0.0.1:8765/web/
```
