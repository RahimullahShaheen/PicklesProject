# Project memory / context notes

Working notes on decisions and environment quirks for this project that
aren't obvious from the code alone. (A copy of this also lives in Claude's
own persistent memory so it's recalled automatically across conversations
without needing this file open — see the note at the bottom.)

## Local environment (this machine)

- **PostgreSQL 17** runs locally as a Windows service. Database `pickles`,
  superuser `postgres` / password `postgres` (trivial local dev default,
  not a real secret).
- **`DATABASE_URL`** is set as a permanent Windows *user* env var:
  `postgresql://postgres:postgres@localhost/pickles`. Gotcha: that only
  applies to terminals opened *after* it was set — any already-open shell
  needs `export DATABASE_URL=...` (or the PowerShell equivalent) run
  manually before `collector.py` / `dashboard.py` / `download_images.py`
  will connect.
- **Norton 360** on this machine intercepts HTTPS traffic (SSL inspection),
  which breaks `pip install` and any `httpx`/`requests` call with
  `CERTIFICATE_VERIFY_FAILED` until Norton's root cert is trusted. Already
  fixed permanently:
  1. Norton's root cert ("Norton Web/Mail Shield Root") was exported from
     the Windows cert store and appended to Python's certifi bundle at
     `...\Python312\site-packages\certifi\cacert.pem` — fixes `httpx` for
     `collector.py` / `download_images.py`.
  2. A permanent `PIP_CERT` user env var points at that same patched
     certifi file — fixes `pip install`, since pip ships its *own separate*
     vendored certifi copy (under `Program Files`, not writable without
     admin) rather than using the one above.
  If SSL errors reappear (e.g. after a Python/pip upgrade replaces
  certifi's `cacert.pem`), reapply the same two steps rather than reaching
  for `--trusted-host` or disabling verification.

## dashboard.py

Streamlit app added 2026-07-16 for browsing collected listings
interactively (filters, sortable table, detail view with images pulled
live from Pickles' CDN, price-history chart). Run with
`streamlit run dashboard.py` (needs `DATABASE_URL` set). First run on a
fresh machine needs `~/.streamlit/credentials.toml` with an empty email
pre-set, otherwise Streamlit blocks on a stdin prompt when launched
non-interactively.

## Two-tier damage sieve: score_damage.py (added 2026-07-18)

Local Tier 1 pass before spending Claude API money on Tier 2 (`appraiser.py`).
`score_damage.py` runs `abdullahg7/cardd-yolov8s` (Hugging Face — YOLOv8s
fine-tuned on the CarDD dataset, 6 damage classes) locally over every active
listing's photos, zero API cost, producing a `damage_scores` row per listing
(detection count + avg damaged-area fraction). Dashboard's new "🎯 Shortlist"
view ranks by that score, **least damage first** (user's choice — easiest-flip
theory, not "find the worst damage").

**GPU gotcha hit and fixed:** this machine has an RTX 3050 with a pre-existing
CUDA-enabled `torch==2.6.0+cu124` (used by some other project — `torchaudio`
was pinned to that exact version). `pip install ultralytics` pulled in a
fresh CPU-only `torch==2.13.0+cpu` as a transitive dependency, silently
downgrading GPU support machine-wide. Fixed by reinstalling the exact prior
version: `pip install torch==2.6.0 torchvision --index-url
https://download.pytorch.org/whl/cu124`. **If any future package pulls in
torch again, check `torch.cuda.is_available()` afterward** — pip's resolver
doesn't protect CUDA builds from being silently replaced by CPU builds.

**Another gotcha:** ultralytics' `model.predict(source=<url>)` downloads
the image to the *current working directory* with no cleanup when given a
URL string — left 36 stray `.jpeg` files in the project root during testing.
Fixed by fetching photos into memory with `httpx` (same pattern as
`appraiser.py`) and passing PIL Image objects to `predict()` instead of raw
URLs — never touches disk.

**Scope note:** the user's original ask assumed ~4,440 listings (a bulk
sieve makes most sense at that scale); the live DB is still the ~207-row
narrowed scope from the collection-scope memory below. User chose to keep
the narrow scope for now — Tier 1 still works as a ranking, just isn't doing
much real sieving at 207 rows. Revisit if they want to widen scope later.

**Model licensing:** AGPL-3.0 (inherited from Ultralytics YOLOv8). Fine for
personal local use; would matter if this were ever hosted as a public
network service.

## appraiser.py — bidding pipeline (added 2026-07-18)

Full workflow: collect (`collector.py`) -> watch (snapshots + `sale_results`)
-> pick a car and price it fixed (`--resale`, from RedBook/carsales) ->
`appraiser.py` sends photos to Claude vision for a facts-only damage read
(no prices) -> `bid_config.json` costs the damage (repair-cost matrix, all
placeholder numbers) -> deterministic bid math computes max bid -> verdict
(`BID` / `INSPECT` / `PARTS_ONLY` / `WALK`) -> saved to the `appraisals`
table for later calibration against `sale_results`.

- **Model:** `claude-sonnet-5` (user chose this over `claude-opus-4-8` for
  cost — was previously `claude-sonnet-4-6`, a real but outdated model, not
  a typo).
- **Storage:** appraisals now save to a Postgres `appraisals` table (added
  to `schema.sql` and applied live), not loose JSON files under
  `./appraisals/` as the script originally did — user chose DB storage
  specifically so the dashboard and future calibration queries could join
  against `sale_results`.
- **Dashboard integration:** `dashboard.py` imports `run_appraisal` /
  `save_appraisal` directly from `appraiser.py` and exposes an "Appraise
  this car" button + resale-price input on each listing's detail view —
  triggers the pipeline live rather than only reading pre-run results.
  Requires `ANTHROPIC_API_KEY` set in the same terminal the dashboard was
  launched from.
- `appraiser.py`'s `main()` was refactored to call a shared
  `run_appraisal(listing, resale, cfg=None) -> (damage, result)` function so
  CLI and dashboard use identical logic.

## Current collection scope (as of 2026-07-16)

`request_payload.json`'s `filter` is hand-tuned to pull a focused subset
instead of the full national index (~4,058 -> ~207 listings):

- **Location:** QLD, Brisbane only.
- **WOVR:** `WOVR N/A` or `Repairable Write-Off` only — excludes
  `Statutory Write-Off` and `Inspection Passed Repairable Writeoff`.
- **Product type:** `Cars` only.
- **Make:** Toyota, Mazda, Mercedes-Benz, Kia, Hyundai, Mitsubishi, Lexus,
  Volkswagen, Zeekr.

Filtering uses the index's `*Filter`/`*Facet` field variants (`makeFilter`,
`productTypeFilter`, `productLocation/stateFilter`,
`productLocation/cityFacetFilter`), not the display fields — e.g.
`makeFilter` collapses both `"Mercedes-Benz"` and `"Mercedes Benz"` (both
appear in raw data) into one canonical value, so filtering on raw `make`
would silently miss records. Verified live against the API before saving.

**The database was fully reset (all tables truncated)** when this scope
was applied. Reason: `collector.py`'s `MARK_DISAPPEARED` query marks any
previously-seen listing absent from the current run as `disappeared_at`
(sold/gone). With the narrower filter, all ~3,800 previously-collected
out-of-scope listings would have been wrongly flagged as disappeared, so
the old broader (nationwide, all-brands) dataset was discarded instead.

**If scope changes again:** don't just edit `request_payload.json` and
rerun. Either reset the DB again, or first scope `MARK_DISAPPEARED`'s
`WHERE` clause to match the active filter so out-of-scope rows are left
alone rather than wrongly marked disappeared. Flag the tradeoff and ask
before acting, same as last time.

---
*Claude also keeps this same context in its own persistent memory
(`.claude/projects/.../memory/`) so it's recalled automatically in future
sessions. If you edit this file directly, mention it in chat so Claude can
keep both copies in sync.*
