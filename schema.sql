-- =====================================================================
-- Pickles Salvage Collector - Postgres schema
-- Designed around the real payload of:
--   POST /api-website/buyer/ms-web-asset-search/v2/api/product/public/search
-- =====================================================================

-- One row per unique vehicle/asset (keyed by stockNumber, which equals "id")
CREATE TABLE IF NOT EXISTS listings (
    stock_number        TEXT PRIMARY KEY,           -- "id" / "stockNumber"
    asset_id            UUID,                       -- "assetId"
    vin                 TEXT,
    title               TEXT,
    make                TEXT,
    model               TEXT,
    series              TEXT,                       -- e.g. "KE1021", "GUN126R"
    badge               TEXT,                       -- e.g. "SR5", "Grand Touring"
    year                INT,
    compliance_date     TEXT,                       -- "03/2013" (keep as text)
    body                TEXT,
    vfacts_class        TEXT,                       -- Passenger / SUV / Light Commercial...
    product_type        TEXT,                       -- Cars / Trucks / EMP...
    colour              TEXT,
    odometer            BIGINT,
    odometer_unit       TEXT,
    engine_capacity_l   NUMERIC,
    cylinders           INT,
    fuel_type           TEXT,
    transmission        TEXT,
    drive_type          TEXT,
    induction           TEXT,

    -- Salvage-analysis critical fields
    wovr                TEXT,                       -- Statutory / Repairable / Inspection Passed / N/A
    incident_types      TEXT[],                     -- {Impact, Fire, Malicious...}
    driveable           BOOLEAN,
    engine_starts       BOOLEAN,
    has_keys            BOOLEAN,
    burnt               BOOLEAN,

    -- Valuation join key (RedBook)
    redbook_code        TEXT,
    redbook_description TEXT,

    -- Location & sale context (latest known)
    state               TEXT,
    city                TEXT,
    suburb              TEXT,
    vendor_name         TEXT,
    buy_method          TEXT,                       -- Pickles Live / Pickles Online
    sale_id             INT,
    sale_name           TEXT,
    sale_start_utc      TIMESTAMPTZ,
    sale_end_utc        TIMESTAMPTZ,
    product_bid_end_utc TIMESTAMPTZ,

    -- Bookkeeping
    first_seen_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    disappeared_at      TIMESTAMPTZ,                -- set when no longer in the index
    raw                 JSONB NOT NULL              -- full original payload, always keep it
);

CREATE INDEX IF NOT EXISTS idx_listings_make_model ON listings (make, model);
CREATE INDEX IF NOT EXISTS idx_listings_wovr        ON listings (wovr);
CREATE INDEX IF NOT EXISTS idx_listings_state       ON listings (state);
CREATE INDEX IF NOT EXISTS idx_listings_bid_end     ON listings (product_bid_end_utc);
CREATE INDEX IF NOT EXISTS idx_listings_redbook     ON listings (redbook_code);

-- Every collection run appends a snapshot per listing: this is how price
-- movement and final results are captured over time.
CREATE TABLE IF NOT EXISTS snapshots (
    id              BIGSERIAL PRIMARY KEY,
    stock_number    TEXT NOT NULL REFERENCES listings(stock_number),
    captured_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    minimum_bid     NUMERIC,
    highest_bid     NUMERIC,
    buy_now_price   NUMERIC,
    price           NUMERIC,
    sale_status     TEXT,                            -- INPREP / PREPCOMPLETED / ...
    for_sale        BOOLEAN,
    product_bid_end TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_snapshots_stock_time ON snapshots (stock_number, captured_at);

-- Inferred/observed sale outcomes. Populated by a post-auction job:
-- last observed highest_bid before the listing disappeared, or a
-- confirmed sold price if one is ever exposed.
CREATE TABLE IF NOT EXISTS sale_results (
    stock_number     TEXT PRIMARY KEY REFERENCES listings(stock_number),
    last_highest_bid NUMERIC,
    result_type      TEXT NOT NULL DEFAULT 'inferred_from_disappearance',
    sale_end_utc     TIMESTAMPTZ,
    recorded_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Image catalogue (URLs always; local_path filled if downloaded)
CREATE TABLE IF NOT EXISTS images (
    image_id      BIGINT PRIMARY KEY,
    stock_number  TEXT NOT NULL REFERENCES listings(stock_number),
    cdn_url       TEXT NOT NULL,
    sequence      INT,
    local_path    TEXT,
    downloaded_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_images_stock ON images (stock_number);

-- One row per appraiser.py run: the AI's damage read, the deterministic bid
-- math, and the resale estimate that was fed in. Lets you later compare
-- max_bid against what similar cars actually sold for (sale_results).
CREATE TABLE IF NOT EXISTS appraisals (
    id              BIGSERIAL PRIMARY KEY,
    stock_number    TEXT NOT NULL REFERENCES listings(stock_number),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    resale_input    NUMERIC NOT NULL,
    verdict         TEXT NOT NULL,             -- BID / INSPECT / PARTS_ONLY / WALK
    max_bid         NUMERIC,
    repair_cost     NUMERIC,
    risk_buffer     NUMERIC,
    damage          JSONB NOT NULL,            -- full Claude damage assessment
    result          JSONB NOT NULL             -- full bid-engine breakdown
);

CREATE INDEX IF NOT EXISTS idx_appraisals_stock ON appraisals (stock_number, created_at);

-- Convenience view: repairable write-offs ending soon (the hunting ground)
CREATE OR REPLACE VIEW v_upcoming_repairables AS
SELECT stock_number, title, year, series, badge, odometer, state, suburb,
       wovr, incident_types, driveable, has_keys, redbook_code,
       product_bid_end_utc, sale_name
FROM listings
WHERE disappeared_at IS NULL
  AND wovr IN ('Repairable Write-Off', 'Inspection Passed Repairable Writeoff', 'WOVR N/A')
  AND product_bid_end_utc > now()
ORDER BY product_bid_end_utc;
