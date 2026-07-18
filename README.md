# Pickles Salvage Collector

A polite, deterministic pipeline for the public search endpoint you found:

```
POST https://www.pickles.com.au/api-website/buyer/ms-web-asset-search/v2/api/product/public/search
```

Backend is Azure Cognitive Search (index `productsearchprd`), proxied publicly
by the Pickles website. Responses include full vehicle detail, WOVR status,
RedBook codes, sale schedules, and direct image CDN URLs.

## Setup

```bash
pip install -r requirements.txt
createdb pickles
psql pickles -f schema.sql
export DATABASE_URL=postgresql://localhost/pickles
```

## The one manual step: capture the request payload

The script needs the JSON *request body* the site sends (it carries the
filter for salvage cars, facet config, etc.):

1. Open the salvage cars search page in Chrome
2. DevTools (F12) -> Network -> filter `search`
3. Click the request to `.../product/public/search`
4. In the **Payload** tab choose "view source", copy the JSON
5. Save it as `request_payload.json` in this folder

The collector reuses that payload verbatim and only overrides the paging
fields (`skip`/`top` or equivalent) each page.

## Run

```bash
python collector.py           # pulls every matching listing, upserts + snapshots
python download_images.py     # optional: fetch images for stored listings
```

Schedule `collector.py` 2-4x daily (cron). Every run:

- upserts listing details (`listings.raw` keeps the full original JSON)
- appends a price/status snapshot (`snapshots`)
- marks listings that vanished from the index (`disappeared_at`)
- records an inferred sale result from the last observed `highestBid`

Salvage auctions run Tue-Fri, so within a few weeks you'll have a real
sold/cleared history — the dataset that actually powers "what should I bid".

## Field notes for the analysis layer

- **`wovr` is the first split for any bidding model.**
  - `Statutory Write-Off`: cannot be re-registered in Australia — parts/
    scrap/export value only. Completely different price ceiling.
  - `Repairable Write-Off`: can be repaired + re-registered after a WOVR
    inspection. This is the classic flip category.
  - `Inspection Passed Repairable Writeoff`: already passed inspection —
    typically commands a premium.
  - `WOVR N/A`: not on the register (e.g. abandoned/council vehicles).
- **`redbookCode`** (e.g. `AUVMAZD2013AEDD`) maps each car to RedBook's
  valuation catalogue — your market-value join key.
- Risk flags: `driveable`, `engineStarts`, `keys`, `burnt`, `incidentType`
  (Impact / Fire / Malicious...), plus free-text inspector notes in
  `otherExtras[].noteText` — good LLM-extraction input.
- `minimumBid` / `highestBid` are mostly 0/null pre-auction. Final prices
  come from re-polling near `productBidEnd` and from the disappearance
  inference. If you register a My Pickles account you may see more bid data,
  but that moves you behind a login and its terms — the public endpoint is
  the safer lane.
- Timezones: `sale.saleStart`/`saleEnd` are UTC with local strings provided.

## Conduct

- Keep `PAGE_DELAY_SECONDS` >= 1.5 and don't hammer the endpoint.
- Review Pickles' Terms of Use; for anything commercial, their developer
  portal (developer.pickles.com.au / Datium Insights) remains the clean path.
- This code is for personal analysis of publicly served data. Don't
  redistribute their content.

## Dashboard (dashboard.py)

A Streamlit app for browsing collected listings interactively instead of
querying Postgres by hand.

```bash
pip install -r requirements.txt   # now also installs streamlit + pandas
export DATABASE_URL=postgresql://localhost/pickles
streamlit run dashboard.py
```

- Sidebar filters: make, WOVR status, state, year range, text search,
  "active only" toggle.
- Click a row in the results table to open a detail panel: full spec sheet,
  images (streamed live from Pickles' CDN — no need to run
  `download_images.py` first), and a price-history line chart built from
  `snapshots` (needs 2+ collector runs to show a real trend).
- First launch on a machine: Streamlit prompts for an email in the terminal
  and blocks waiting for input if run non-interactively. Pre-empt it by
  creating `~/.streamlit/credentials.toml` with `[general]\nemail = ""`.
- Also shows an "Appraise this car" panel on each listing (see below) —
  images used there prefer local files from `download_images.py`
  (`images/<stock_number>/<image_id>.jpg`) and fall back to the live CDN URL
  for anything not downloaded yet.

## Appraiser (appraiser.py) — the bidding pipeline

The full workflow this project supports, end to end:

1. **Collect** (`collector.py`) — pulls every matching listing on a schedule.
2. **Watch** — every run appends a price/status snapshot; when a listing
   disappears post-auction it's marked sold and the last observed bid is
   recorded in `sale_results`. A few weeks of this gives you real sold-price
   history for the scope you're tracking.
3. **Pick a car and price it fixed** — find comparable sold prices for the
   *repaired* car yourself (RedBook/carsales). That number is `--resale`.
4. **Appraise** — `appraiser.py` sends that listing's photos + description to
   Claude with strict instructions to report only what's visible in the
   photos (damaged panels, airbags, structural/frame concerns, fire/water
   damage) — no pricing, no guessing beyond the images.
5. **Cost the damage** — `bid_config.json` holds your repair-cost matrix
   (panel repair/replace, paint, airbags, glass, wheels, mechanical,
   interior). All placeholder numbers — replace with your real rates and
   Pickles' current fee schedule.
6. **Compute the max bid** — deterministic arithmetic, not AI: resale value
   minus your target margin, repairs, buyer fees, transport, WOVR
   inspection/rego, and a risk buffer (bigger if the engine doesn't start,
   keys are missing, odometer unknown, etc.).
7. **Verdict** — one of `BID` (with a max number), `INSPECT` (structural
   damage suspected — see it in person first), `PARTS_ONLY` (Statutory
   Write-Off — can never be re-registered), or `WALK` (fire/flood, or the
   math doesn't work).
8. **Calibrate** — every appraisal is saved to the `appraisals` table
   (stock number, resale input, verdict, max bid, full damage JSON, full
   bid breakdown). Once you have sold-price history from step 2, compare
   `appraisals.max_bid` against `sale_results.last_highest_bid` for similar
   cars to find where your numbers run high or low, and adjust
   `bid_config.json` accordingly.

```bash
export ANTHROPIC_API_KEY=sk-ant-...
export DATABASE_URL=postgresql://localhost/pickles

python appraiser.py --stock 62336544 --resale 21500   # from the collector DB
python appraiser.py --json listing.json --resale 21500  # or a saved raw listing
```

**Or from the dashboard:** open a listing, enter your resale estimate in the
"Appraisal" panel, and click "Run appraisal" — same pipeline, triggered live,
result saved to the DB and shown inline (verdict, repair line items, max
bid). Needs `ANTHROPIC_API_KEY` set in the same terminal the dashboard was
launched from.

Uses `claude-sonnet-5` for the vision step (good damage-assessment quality
at Sonnet cost — swap `MODEL` in `appraiser.py` for `claude-opus-4-8` if you
want maximum accuracy on subtle damage and don't mind ~5-8x the cost).

## Current collection scope

`request_payload.json`'s `filter` is currently narrowed (edited by hand,
not by the script) to only pull:

- **Location:** QLD, Brisbane only — `productLocation/stateFilter eq 'QLD'`
  and `productLocation/cityFacetFilter eq 'QLD|Brisbane'`.
- **WOVR:** `WOVR N/A` or `Repairable Write-Off` only — excludes
  `Statutory Write-Off` and `Inspection Passed Repairable Writeoff`.
- **Product type:** `Cars` only (`productTypeFilter eq 'Cars'`) — excludes
  Trucks, Motorcycles, Caravans, Boats, Earthmoving/Trailers.
- **Make:** Toyota, Mazda, Mercedes-Benz, Kia, Hyundai, Mitsubishi, Lexus,
  Volkswagen, Zeekr — via `makeFilter`, which normalizes spelling variants
  (Pickles' raw `make` field has both `"Mercedes-Benz"` and
  `"Mercedes Benz"`; `makeFilter` collapses both to `"Mercedes-Benz"`).

This filter was validated directly against the live API (200 OK, results
matched all criteria) before being saved.

**Field-name note:** the search index exposes both a display field (e.g.
`make`, `productType/title`, `productLocation/city`) and a separate
filter-safe field (`makeFilter`, `productTypeFilter`,
`productLocation/stateFilter`, `productLocation/cityFacetFilter`). Always
filter on the `*Filter`/`*Facet` variant, not the display field, to avoid
missing records due to spelling/formatting differences.

**If you widen or change this filter again:** the database was reset
(all tables truncated) the first time this narrow filter was applied,
because `collect()`'s disappearance logic (`MARK_DISAPPEARED` in
`collector.py`) marks *any* previously-seen listing absent from the current
run's results as sold/gone (`disappeared_at`). A narrower filter makes
every out-of-scope listing look like it vanished. Before changing scope
again, either reset the DB again, or first scope `MARK_DISAPPEARED`'s
`WHERE` clause to match the filter criteria so out-of-scope rows are left
alone instead of wrongly marked disappeared.

## Local environment notes (this machine)

- **PostgreSQL 17** runs as a Windows service. Database `pickles`, user
  `postgres`, password `postgres` (local dev default — not a real secret).
  `DATABASE_URL` is set as a permanent Windows user env var
  (`postgresql://postgres:postgres@localhost/pickles`), but that only
  applies to *new* terminals opened after it was set — existing shells need
  `export DATABASE_URL=...` run manually.
- **Norton 360** intercepts HTTPS traffic on this machine (SSL inspection),
  which breaks `pip install` and any `httpx`/`requests` call with
  `CERTIFICATE_VERIFY_FAILED` until Norton's root cert is trusted. Fixed
  permanently by:
  1. Exporting Norton's root cert ("Norton Web/Mail Shield Root") from
     the Windows cert store and appending it to Python's certifi bundle
     (`...\Python312\site-packages\certifi\cacert.pem`) — fixes `httpx`
     for `collector.py`/`download_images.py`.
  2. Setting a permanent `PIP_CERT` user env var pointing at that same
     patched certifi bundle — fixes `pip install`, since pip ships its
     *own separate* vendored certifi copy (under `Program Files`, not
     writable without admin) rather than using the one above.
  If SSL errors reappear (e.g. after a Python/pip upgrade replaces the
  certifi file), reapply the same fix.
