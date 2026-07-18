"""
Downloads listing images recorded by collector.py.

Images live on Pickles' CDN (two URL styles appear in the payload:
www.pickles.com.au/images/transform/... and images.pickles.com.au/image/upload/...).
Both are directly fetchable. Downloads are concurrent but throttled.

Usage:
    export DATABASE_URL=postgresql://localhost/pickles
    python download_images.py --limit 500 --out ./images
"""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

import httpx
import psycopg

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://localhost/pickles")
CONCURRENCY = 5          # simultaneous downloads - keep this low and polite
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Referer": "https://www.pickles.com.au/",
}


async def fetch_one(client: httpx.AsyncClient, sem: asyncio.Semaphore,
                    row: tuple, out_dir: Path) -> tuple[int, str] | None:
    image_id, stock_number, cdn_url = row
    dest_dir = out_dir / stock_number
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{image_id}.jpg"
    if dest.exists():
        return image_id, str(dest)
    async with sem:
        try:
            resp = await client.get(cdn_url)
            resp.raise_for_status()
            dest.write_bytes(resp.content)
            await asyncio.sleep(0.3)  # politeness
            return image_id, str(dest)
        except httpx.HTTPError as exc:
            print(f"  failed {image_id}: {exc}")
            return None


async def main(limit: int, out: str) -> None:
    out_dir = Path(out)
    with psycopg.connect(DATABASE_URL) as conn:
        rows = conn.execute(
            """SELECT image_id, stock_number, cdn_url
               FROM images WHERE local_path IS NULL
               ORDER BY stock_number LIMIT %s""",
            (limit,),
        ).fetchall()

    if not rows:
        print("Nothing to download.")
        return
    print(f"Downloading {len(rows)} images...")

    sem = asyncio.Semaphore(CONCURRENCY)
    async with httpx.AsyncClient(headers=HEADERS, timeout=30,
                                 follow_redirects=True) as client:
        results = await asyncio.gather(
            *(fetch_one(client, sem, r, out_dir) for r in rows)
        )

    done = [r for r in results if r]
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.executemany(
                "UPDATE images SET local_path=%s, downloaded_at=now() "
                "WHERE image_id=%s",
                [(path, image_id) for image_id, path in done],
            )
        conn.commit()
    print(f"Saved {len(done)}/{len(rows)} images to {out_dir}/")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=500)
    p.add_argument("--out", default="./images")
    args = p.parse_args()
    asyncio.run(main(args.limit, args.out))
