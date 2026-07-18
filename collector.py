"""
Pickles Salvage Collector
=========================
Paginates the public search endpoint you captured:

    POST https://www.pickles.com.au/api-website/buyer/ms-web-asset-search/v2/api/product/public/search

and upserts every listing into Postgres, appending a price/status snapshot
per run. Run it on a schedule (e.g. every 6-12 hours) and your sold-price
history builds itself.

SETUP
-----
1.  pip install -r requirements.txt
2.  createdb pickles && psql pickles -f schema.sql
3.  In Chrome DevTools > Network, click the search request and copy its
    *request payload* (the JSON body) into request_payload.json.
    The script overrides the paging fields on that payload each page.
4.  export DATABASE_URL=postgresql://localhost/pickles
5.  python collector.py

Be a polite client: keep PAGE_DELAY_SECONDS >= 1.5 and don't run more than
a few times per day. Respect the site.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import psycopg
from psycopg.types.json import Jsonb

# ----------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------
SEARCH_URL = (
    "https://www.pickles.com.au/api-website/buyer/"
    "ms-web-asset-search/v2/api/product/public/search"
)
PAGE_SIZE = 50                 # keep modest; the site UI uses similar sizes
PAGE_DELAY_SECONDS = 2.0       # politeness delay between page requests
PAYLOAD_FILE = Path(__file__).parent / "request_payload.json"
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://localhost/pickles")

HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Origin": "https://www.pickles.com.au",
    "Referer": "https://www.pickles.com.au/used/search/lob/salvage/cars",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
}

# Common paging key names seen on Azure Cognitive Search proxies.
# The script tries these in order against your captured payload.
PAGING_KEY_CANDIDATES = [("skip", "top"), ("$skip", "$top"), ("from", "size")]


def load_base_payload() -> dict:
    """Load the request body you copied from DevTools."""
    if not PAYLOAD_FILE.exists():
        sys.exit(
            f"Missing {PAYLOAD_FILE.name}.\n"
            "Open DevTools > Network, click the 'search' request, copy the "
            "request payload JSON, and save it as request_payload.json."
        )
    return json.loads(PAYLOAD_FILE.read_text())


def detect_paging_keys(payload: dict) -> tuple[str, str]:
    for skip_key, top_key in PAGING_KEY_CANDIDATES:
        if skip_key in payload or top_key in payload:
            return skip_key, top_key
    # Fall back to the most common convention
    return "skip", "top"


def parse_ts(value: str | None):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


# ----------------------------------------------------------------------
# Database upserts
# ----------------------------------------------------------------------
UPSERT_LISTING = """
INSERT INTO listings (
    stock_number, asset_id, vin, title, make, model, series, badge, year,
    compliance_date, body, vfacts_class, product_type, colour,
    odometer, odometer_unit, engine_capacity_l, cylinders, fuel_type,
    transmission, drive_type, induction,
    wovr, incident_types, driveable, engine_starts, has_keys, burnt,
    redbook_code, redbook_description,
    state, city, suburb, vendor_name, buy_method,
    sale_id, sale_name, sale_start_utc, sale_end_utc, product_bid_end_utc,
    last_seen_at, disappeared_at, raw
) VALUES (
    %(stock_number)s, %(asset_id)s, %(vin)s, %(title)s, %(make)s, %(model)s,
    %(series)s, %(badge)s, %(year)s, %(compliance_date)s, %(body)s,
    %(vfacts_class)s, %(product_type)s, %(colour)s,
    %(odometer)s, %(odometer_unit)s, %(engine_capacity_l)s, %(cylinders)s,
    %(fuel_type)s, %(transmission)s, %(drive_type)s, %(induction)s,
    %(wovr)s, %(incident_types)s, %(driveable)s, %(engine_starts)s,
    %(has_keys)s, %(burnt)s, %(redbook_code)s, %(redbook_description)s,
    %(state)s, %(city)s, %(suburb)s, %(vendor_name)s, %(buy_method)s,
    %(sale_id)s, %(sale_name)s, %(sale_start_utc)s, %(sale_end_utc)s,
    %(product_bid_end_utc)s, now(), NULL, %(raw)s
)
ON CONFLICT (stock_number) DO UPDATE SET
    last_seen_at = now(),
    disappeared_at = NULL,
    highest_seen_raw_update = TRUE
"""

# psycopg doesn't like the dummy column trick; build the real update instead:
UPSERT_LISTING = UPSERT_LISTING.replace(
    "    highest_seen_raw_update = TRUE",
    "    sale_id = EXCLUDED.sale_id,\n"
    "    sale_name = EXCLUDED.sale_name,\n"
    "    sale_start_utc = EXCLUDED.sale_start_utc,\n"
    "    sale_end_utc = EXCLUDED.sale_end_utc,\n"
    "    product_bid_end_utc = EXCLUDED.product_bid_end_utc,\n"
    "    buy_method = EXCLUDED.buy_method,\n"
    "    state = EXCLUDED.state,\n"
    "    city = EXCLUDED.city,\n"
    "    suburb = EXCLUDED.suburb,\n"
    "    wovr = EXCLUDED.wovr,\n"
    "    odometer = COALESCE(EXCLUDED.odometer, listings.odometer),\n"
    "    raw = EXCLUDED.raw",
)

INSERT_SNAPSHOT = """
INSERT INTO snapshots (stock_number, minimum_bid, highest_bid, buy_now_price,
                       price, sale_status, for_sale, product_bid_end)
VALUES (%(stock_number)s, %(minimum_bid)s, %(highest_bid)s, %(buy_now_price)s,
        %(price)s, %(sale_status)s, %(for_sale)s, %(product_bid_end)s)
"""

UPSERT_IMAGE = """
INSERT INTO images (image_id, stock_number, cdn_url, sequence)
VALUES (%(image_id)s, %(stock_number)s, %(cdn_url)s, %(sequence)s)
ON CONFLICT (image_id) DO NOTHING
"""

MARK_DISAPPEARED = """
UPDATE listings
SET disappeared_at = now()
WHERE disappeared_at IS NULL
  AND stock_number <> ALL(%(seen)s)
"""

RECORD_RESULTS = """
INSERT INTO sale_results (stock_number, last_highest_bid, sale_end_utc)
SELECT l.stock_number,
       (SELECT s.highest_bid FROM snapshots s
         WHERE s.stock_number = l.stock_number
         ORDER BY s.captured_at DESC LIMIT 1),
       l.product_bid_end_utc
FROM listings l
WHERE l.disappeared_at IS NOT NULL
ON CONFLICT (stock_number) DO NOTHING
"""


def row_from_item(item: dict) -> dict:
    loc = item.get("productLocation") or {}
    sale = item.get("sale") or {}
    ptype = item.get("productType") or {}
    return {
        "stock_number": str(item.get("stockNumber") or item.get("id")),
        "asset_id": item.get("assetId"),
        "vin": item.get("vin"),
        "title": item.get("title"),
        "make": item.get("make"),
        "model": item.get("model"),
        "series": item.get("series"),
        "badge": item.get("badge"),
        "year": item.get("year"),
        "compliance_date": item.get("complianceDate"),
        "body": item.get("body"),
        "vfacts_class": item.get("vFactsClass"),
        "product_type": ptype.get("title"),
        "colour": item.get("colour"),
        "odometer": item.get("odometer"),
        "odometer_unit": item.get("odometerUnit"),
        "engine_capacity_l": item.get("engineCapacityInLitres"),
        "cylinders": item.get("cylinders"),
        "fuel_type": item.get("fuelType"),
        "transmission": item.get("transmission"),
        "drive_type": item.get("driveType"),
        "induction": item.get("induction"),
        "wovr": item.get("wovr"),
        "incident_types": item.get("incidentType") or [],
        "driveable": item.get("driveable"),
        "engine_starts": item.get("engineStarts"),
        "has_keys": item.get("keys"),
        "burnt": item.get("burnt"),
        "redbook_code": item.get("redbookCode"),
        "redbook_description": item.get("redbookDescription"),
        "state": loc.get("state"),
        "city": loc.get("city"),
        "suburb": loc.get("suburb"),
        "vendor_name": item.get("vendorName"),
        "buy_method": item.get("buyMethod"),
        "sale_id": sale.get("saleId"),
        "sale_name": (sale.get("name") or "").strip() or None,
        "sale_start_utc": parse_ts(sale.get("saleStart")),
        "sale_end_utc": parse_ts(sale.get("saleEnd")),
        "product_bid_end_utc": parse_ts(item.get("productBidEnd")),
        "raw": Jsonb(item),
    }


def snapshot_from_item(item: dict) -> dict:
    sale = item.get("sale") or {}
    return {
        "stock_number": str(item.get("stockNumber") or item.get("id")),
        "minimum_bid": item.get("minimumBid"),
        "highest_bid": item.get("highestBid"),
        "buy_now_price": item.get("buyNowPrice"),
        "price": item.get("price"),
        "sale_status": sale.get("status"),
        "for_sale": item.get("forSale"),
        "product_bid_end": parse_ts(item.get("productBidEnd")),
    }


# ----------------------------------------------------------------------
# Main collection loop
# ----------------------------------------------------------------------
def collect() -> None:
    base_payload = load_base_payload()
    skip_key, top_key = detect_paging_keys(base_payload)
    print(f"Paging with keys: {skip_key}/{top_key}")

    seen: list[str] = []
    started = datetime.now(timezone.utc)

    with (
        httpx.Client(headers=HEADERS, timeout=30) as client,
        psycopg.connect(DATABASE_URL) as conn,
    ):
        skip = 0
        total = None
        while True:
            payload = dict(base_payload)
            payload[skip_key] = skip
            payload[top_key] = PAGE_SIZE

            resp = client.post(SEARCH_URL, json=payload)
            resp.raise_for_status()
            data = resp.json()

            items = data.get("value", [])
            if total is None:
                total = data.get("@odata.count")
                print(f"Index reports {total} matching items")
            if not items:
                break

            with conn.cursor() as cur:
                for item in items:
                    row = row_from_item(item)
                    seen.append(row["stock_number"])
                    cur.execute(UPSERT_LISTING, row)
                    cur.execute(INSERT_SNAPSHOT, snapshot_from_item(item))
                    for img in item.get("images") or []:
                        cur.execute(
                            UPSERT_IMAGE,
                            {
                                "image_id": img.get("imageId"),
                                "stock_number": row["stock_number"],
                                "cdn_url": img.get("cdnUrl"),
                                "sequence": img.get("sequence"),
                            },
                        )
            conn.commit()

            print(f"  upserted {len(items)} items (offset {skip})")
            skip += PAGE_SIZE
            if total is not None and skip >= int(total):
                break
            time.sleep(PAGE_DELAY_SECONDS)

        # Anything previously live that we did NOT see this run has left
        # the index (sold, withdrawn, or relisted) -> mark + record result.
        with conn.cursor() as cur:
            cur.execute(MARK_DISAPPEARED, {"seen": seen})
            gone = cur.rowcount
            cur.execute(RECORD_RESULTS)
        conn.commit()
        print(f"Marked {gone} listings as disappeared this run.")

    print(f"Done in {(datetime.now(timezone.utc) - started).seconds}s, "
          f"{len(seen)} listings processed.")


if __name__ == "__main__":
    collect()
