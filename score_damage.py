"""
Tier 1 bulk sieve: local YOLOv8s damage detector.
================================================
"Local model does the sieving, Claude only appraises the shortlist."

Scores every current listing's photos for visible damage (instance count +
damaged area) using abdullahg7/cardd-yolov8s, a YOLOv8s model fine-tuned on
the CarDD dataset (6 classes: dent, scratch, crack, glass_shatter,
lamp_broken, tire_flat). Runs locally at zero API cost, on GPU if available.

Results are stored in `damage_scores` so the dashboard's Shortlist view can
rank the current collection scope by damage severity. Only the resulting
shortlist should go through appraiser.py (Tier 2, Claude vision) — that's
where the real per-car API cost is, so spend it only on cars worth a bid.

Usage:
    export DATABASE_URL=postgresql://localhost/pickles
    python score_damage.py               # score listings not yet scored
    python score_damage.py --rescan       # rescan everything
    python score_damage.py --limit 50     # cap how many listings this run
"""

from __future__ import annotations

import argparse
import io
import json
import os

import httpx
import psycopg
import torch
from huggingface_hub import hf_hub_download
from PIL import Image
from ultralytics import YOLO

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://localhost/pickles")
MAX_IMAGES = 12  # first N photos by sequence, same cap as appraiser.py
MODEL_REPO = "abdullahg7/cardd-yolov8s"
MODEL_FILE = "v2.0/best.pt"  # detection + segmentation
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://www.pickles.com.au/",
}


def load_model() -> YOLO:
    device = "cuda:0" if torch.cuda.is_available() else "cpu"
    print(f"Loading {MODEL_REPO} ({MODEL_FILE}) on {device}...")
    weights_path = hf_hub_download(repo_id=MODEL_REPO, filename=MODEL_FILE)
    model = YOLO(weights_path)
    model.to(device)
    return model


def listings_to_score(conn: psycopg.Connection, rescan: bool, limit: int | None) -> list[str]:
    if rescan:
        query = "SELECT stock_number FROM listings WHERE disappeared_at IS NULL"
    else:
        query = """
            SELECT l.stock_number FROM listings l
            LEFT JOIN damage_scores d ON d.stock_number = l.stock_number
            WHERE l.disappeared_at IS NULL AND d.stock_number IS NULL
        """
    if limit:
        query += f" LIMIT {int(limit)}"
    return [r[0] for r in conn.execute(query).fetchall()]


def image_urls(conn: psycopg.Connection, stock_number: str) -> list[str]:
    rows = conn.execute(
        "SELECT cdn_url FROM images WHERE stock_number=%s ORDER BY sequence LIMIT %s",
        (stock_number, MAX_IMAGES),
    ).fetchall()
    return [r[0] for r in rows]


def fetch_image(client: httpx.Client, url: str) -> Image.Image | None:
    """Fetch a photo into memory — avoids ultralytics writing URL sources to disk."""
    try:
        resp = client.get(url)
        resp.raise_for_status()
        return Image.open(io.BytesIO(resp.content)).convert("RGB")
    except (httpx.HTTPError, OSError) as exc:
        print(f"    (skipped one image: {exc})")
        return None


def score_listing(model: YOLO, urls: list[str]) -> dict:
    """Run YOLO over a listing's photos and aggregate a damage score."""
    class_counts: dict[str, int] = {}
    per_image_area: list[float] = []
    detection_count = 0
    images_scanned = 0

    with httpx.Client(timeout=30, follow_redirects=True, headers=HEADERS) as client:
        images = [img for url in urls if (img := fetch_image(client, url)) is not None]

    for image in images:
        results = model.predict(source=image, conf=0.25, iou=0.45, verbose=False)
        images_scanned += 1
        result = results[0]
        boxes = result.boxes
        if boxes is None or len(boxes) == 0:
            per_image_area.append(0.0)
            continue
        names = result.names
        area = 0.0
        for cls_id, xywhn in zip(boxes.cls.tolist(), boxes.xywhn.tolist()):
            label = names[int(cls_id)]
            class_counts[label] = class_counts.get(label, 0) + 1
            detection_count += 1
            area += xywhn[2] * xywhn[3]  # normalized width * height
        per_image_area.append(area)

    area_score = sum(per_image_area) / len(per_image_area) if per_image_area else 0.0
    return {
        "images_scanned": images_scanned,
        "detection_count": detection_count,
        "area_score": area_score,
        "class_counts": class_counts,
    }


def save_score(conn: psycopg.Connection, stock_number: str, score: dict) -> None:
    conn.execute(
        """INSERT INTO damage_scores
             (stock_number, images_scanned, detection_count, area_score, class_counts)
           VALUES (%s, %s, %s, %s, %s)
           ON CONFLICT (stock_number) DO UPDATE SET
             scored_at = now(),
             images_scanned = EXCLUDED.images_scanned,
             detection_count = EXCLUDED.detection_count,
             area_score = EXCLUDED.area_score,
             class_counts = EXCLUDED.class_counts""",
        (
            stock_number, score["images_scanned"], score["detection_count"],
            score["area_score"], json.dumps(score["class_counts"]),
        ),
    )
    conn.commit()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rescan", action="store_true", help="rescan every active listing")
    ap.add_argument("--limit", type=int, help="cap how many listings this run scores")
    args = ap.parse_args()

    model = load_model()

    with psycopg.connect(DATABASE_URL) as conn:
        stocks = listings_to_score(conn, args.rescan, args.limit)
        print(f"Scoring {len(stocks)} listing(s)...")

        for i, stock in enumerate(stocks, 1):
            urls = image_urls(conn, stock)
            if not urls:
                print(f"  [{i}/{len(stocks)}] {stock}: no images, skipping")
                continue
            score = score_listing(model, urls)
            save_score(conn, stock, score)
            print(
                f"  [{i}/{len(stocks)}] {stock}: {score['images_scanned']} photos, "
                f"{score['detection_count']} detections, "
                f"area_score={score['area_score']:.4f}"
            )

    print(f"\nDone. Scored {len(stocks)} listing(s). "
          f"Open the dashboard's Shortlist view to see the ranking.")


if __name__ == "__main__":
    main()
