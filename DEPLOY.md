# Deploying to GitHub Pages (with a working Refresh button)

This is the production deployment recipe — frontend on GitHub Pages, scrapers
on a GitHub Actions cron, and the in-page **Refresh** button wired to
trigger the same Actions workflow on demand via the GitHub REST API.

## What you get

- A public URL at `https://<you>.github.io/<repo>/web/`
- Weekly automatic refresh every Monday at 07:00 PT (GitHub Actions cron in
  `.github/workflows/refresh.yml`)
- A working **Refresh** button on the page that triggers the same workflow
  on demand. Polls the run, waits for Pages to publish, then reloads the
  calendar.
- Zero ongoing infrastructure cost.

## One-time setup

### 1. Push the repo to GitHub

```bash
cd bay-area-courts
git init
git add .
git commit -m "initial commit"
git branch -M main
git remote add origin git@github.com:<you>/<repo>.git
git push -u origin main
```

> Replace `<you>` and `<repo>` everywhere below.

### 2. Enable GitHub Pages

GitHub → repo → **Settings → Pages** →
- **Source**: Deploy from a branch
- **Branch**: `main`, folder: `/ (root)`
- Save

After ~30 seconds the site is at `https://<you>.github.io/<repo>/web/`.
You can also see the Pages deploy under **Actions → pages-build-deployment**.

### 3. Run the workflow once to populate `data/merged.json`

GitHub → repo → **Actions → "Refresh court data" → Run workflow**.

The first run takes ~5 minutes:
- ~30 s setup (Python, Chromium, EasyOCR cache miss the very first time)
- ~3 minutes scraping (rate-limited 1s/req)
- ~1 minute merging + committing
- ~1 minute Pages re-deploy

When the green check appears, refresh the page — you should see populated
events from all four sources.

### 4. Generate a Personal Access Token for the in-page Refresh button

The Refresh button on the deployed site needs to call the GitHub REST API
to trigger the workflow. Browsers can't do this without an API token.

Go to **Settings → Developer settings → Personal access tokens →
Fine-grained tokens → Generate new token**:

| Field | Value |
|---|---|
| Token name | `bay-area-courts refresh` (any) |
| Expiration | Pick whatever you're OK rotating (90 days is fine) |
| Resource owner | Your account |
| Repository access | **Only select repositories**, pick this one |
| Repository permissions → **Actions** | **Read and write** |
| Repository permissions → **Metadata** | Read-only (auto-required) |
| Everything else | Leave default (no access) |

Click **Generate token** and copy it (it starts with `github_pat_`). You
won't be able to see it again.

> The token can do exactly two things: trigger this one workflow and read
> its run status. It cannot push code, read secrets, or touch other repos.
> Worst-case if it leaks: someone burns your free Actions minutes (which
> are unlimited for public repos).

### 5. Use the token in the browser

1. Open `https://<you>.github.io/<repo>/web/`
2. Click **Refresh**
3. The browser prompts for a token. Paste the one from step 4.
4. The token is stored in `localStorage` (key `gh_pat`) and never sent
   anywhere except `api.github.com`.

The page status line will walk through:
```
Refreshing…
Looking up workflow…
Triggering GitHub Actions workflow…
Waiting for workflow to start…
Workflow in_progress · 60s · run #42
Workflow in_progress · 120s · run #42
Workflow completed · success · 180s · run #42
Workflow done — waiting for Pages to publish…
Refreshed via GitHub Actions · run #42
```

Total: ~5 minutes from click to fresh data. Subsequent clicks reuse the
stored token, no prompt.

If the token gets rejected (revoked, expired, wrong scope) the page clears
the bad token and prompts for a new one on the next click.

## Auto-detection

The frontend reads `web/config.js` then auto-detects the GitHub repo from
the page URL. The default `<you>.github.io/<repo>/web/` Just Works™.

If you serve the site from a custom domain, edit `web/config.js`:

```js
window.SITE_CONFIG = {
  repoOwner: "wentao",
  repoName:  "bay-area-courts",
  workflowFile: "refresh.yml",
  workflowRef:  "main",
};
```

## How the workflow lays it out

```
.github/workflows/refresh.yml
  ├─ on: schedule (cron Monday 14:00 UTC) + workflow_dispatch
  ├─ permissions: contents:write
  └─ steps:
       checkout → setup-python(cache=pip) → cache EasyOCR models →
       pip install -r requirements.txt → playwright install chromium →
       python scripts/run_all_scrapers.py → python scripts/merge.py →
       commit data/raw + data/merged.json + data/raw/images
```

The commit step is a no-op when nothing changed (`git diff --staged
--quiet` guard), so the repo doesn't fill up with empty refresh commits.

## How the in-page Refresh button decides what to do

`web/app.js#refresh()` tries two paths in order:

1. **Local fast path** — `POST /api/refresh` (handled by
   `scripts/serve.py`). On a static host this 404s and we fall through.
2. **GitHub Actions path** — `POST .../actions/workflows/refresh.yml/dispatches`
   with the user's token, then poll until the run is `completed/success`,
   then sleep 30 s for Pages to publish, then `loadMergedJSON()`.

So the same Refresh button works for:
- **Local development** (`python scripts/serve.py`) — full re-scrape on
  this machine in ~2 minutes
- **GitHub Pages** — trigger the workflow in ~5 minutes

## Troubleshooting

### "Can't detect GitHub repo"
You're serving from a custom domain or subpath. Set `repoOwner` and
`repoName` in `web/config.js`.

### "GitHub rejected the token (401/403)"
Token expired, was revoked, or doesn't have **Actions: read and write** on
this specific repo. The page clears it; click Refresh again to enter a
fresh one.

### "no new workflow run appeared after dispatch"
Either the workflow file isn't named `refresh.yml`, or the default branch
isn't `main`. Update `workflowFile` / `workflowRef` in `web/config.js`.

### "workflow_dispatch HTTP 422"
GitHub's API requires the workflow to have run at least once before it
appears in the dispatchable workflows list. Click **Run workflow** in the
Actions tab manually first (step 3 above).

### Workflow runs but Pages isn't updating
Check **Settings → Pages** — make sure the source is still set to your
default branch. After the cron commits, Pages should auto-deploy within
~1 minute. The Actions tab shows a `pages-build-deployment` run for each
deploy.

### EasyOCR download is slow on the first run
The first run downloads ~100 MB of models. Subsequent runs read from
`actions/cache@v4` and are instant. If the cache misses, that step takes
~30 s extra — not catastrophic but annoying.

## Local development still works the same way

```bash
pip install -r requirements.txt
python -m playwright install --with-deps chromium
python scripts/serve.py
# → http://127.0.0.1:8765/web/
```

The Refresh button on `127.0.0.1` hits `/api/refresh` (faster, no token
needed). Same UI, same code path selection logic.
