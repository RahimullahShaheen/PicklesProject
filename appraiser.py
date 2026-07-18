"""
Salvage Appraiser
=================
"AI estimates facts, math computes the price."

Given a Pickles listing (from your collector DB, or a pasted raw JSON file),
this script:

  1. Sends the listing photos + description + inspector notes to Claude
     (vision) with a strict rubric -> structured damage JSON. No prices.
  2. Costs the damage against YOUR repair-cost matrix (bid_config.json).
  3. Computes a maximum profitable bid deterministically, with hard gates:
       - Statutory Write-Off        -> PARTS_ONLY (never a repair bid)
       - Structural damage suspected -> INSPECT (no blind bid)
  4. Prints a full auditable breakdown.

Usage:
    export ANTHROPIC_API_KEY=sk-ant-...
    export DATABASE_URL=postgresql://localhost/pickles

    # From your collector database:
    python appraiser.py --stock 62336544 --resale 21500

    # Or from a saved raw listing JSON:
    python appraiser.py --json listing.json --resale 21500

--resale is YOUR estimate of the repaired car's realistic sale price
(RedBook / carsales comparables). Automating this via your sold-price DB
is the next layer.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from pathlib import Path

import httpx

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
MODEL = "claude-sonnet-5"
MAX_IMAGES = 12          # first N photos by sequence; raise for tricky cars
CONFIG_FILE = Path(__file__).parent / "bid_config.json"
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://localhost/pickles")

DAMAGE_SCHEMA_INSTRUCTIONS = """
You are a senior Australian salvage-vehicle assessor. You will receive
auction photos of a damaged vehicle plus its listing description and any
inspector notes. Assess ONLY what is observable. Do NOT estimate any prices
or values. Respond ONLY with a JSON object, no markdown fences, no preamble,
matching exactly this schema:

{
  "panels": [
    {"name": "<e.g. front bumper, bonnet, left front guard, roof>",
     "action": "repair" | "replace",
     "severity": 1-5}
  ],
  "airbags_deployed": true | false | "unclear",
  "airbag_evidence": "<what you saw>",
  "structural_damage_suspected": true | false,
  "structural_evidence": "<crush zones, rail deformation, pillar damage, gaps>",
  "glass_damaged": ["windscreen", "left front door glass", ...],
  "wheels_damaged": 0-4,
  "lights_damaged": ["left headlight", ...],
  "interior_condition": "clean" | "worn" | "damaged" | "stripped" | "unclear",
  "fire_damage": true | false,
  "water_flood_suspected": true | false,
  "mechanical_flags": ["fluid leak visible", "engine bay impact", ...],
  "drivetrain_impact_risk": "low" | "medium" | "high",
  "description_photo_mismatch": "<anything the photos show that the listing
                                  understates, or 'none'>",
  "hidden_damage_risk": 0.0-1.0,
  "overall_severity": 1-5,
  "summary": "<2-3 sentences, factual>"
}

Rules:
- Deployed airbags: look for visible bags, blown steering-wheel/dash covers,
  dropped headliners. If curtain airbags deployed, note it explicitly.
- structural_damage_suspected = true if impact appears to reach chassis
  rails, strut towers, pillars, or floor, or panel gaps suggest frame shift.
- Be conservative: if photos are insufficient to rule something out, raise
  hidden_damage_risk rather than guessing optimistically.
"""


# ----------------------------------------------------------------------
# Listing + image loading
# ----------------------------------------------------------------------
def load_listing_from_db(stock: str) -> dict:
    import psycopg
    db = os.environ.get("DATABASE_URL", "postgresql://localhost/pickles")
    with psycopg.connect(db) as conn:
        row = conn.execute(
            "SELECT raw FROM listings WHERE stock_number = %s", (stock,)
        ).fetchone()
    if not row:
        sys.exit(f"Stock {stock} not found in DB. Run collector.py first.")
    return row[0]


def fetch_images_b64(listing: dict, limit: int = MAX_IMAGES) -> list[dict]:
    images = sorted(listing.get("images") or [],
                    key=lambda i: i.get("sequence") or 999)[:limit]
    blocks = []
    with httpx.Client(timeout=30, follow_redirects=True,
                      headers={"User-Agent": "Mozilla/5.0",
                               "Referer": "https://www.pickles.com.au/"}) as c:
        for img in images:
            try:
                r = c.get(img["cdnUrl"])
                r.raise_for_status()
                blocks.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": base64.b64encode(r.content).decode(),
                    },
                })
            except httpx.HTTPError as e:
                print(f"  (skipped one image: {e})")
    if not blocks:
        sys.exit("Could not fetch any images for this listing.")
    return blocks


def listing_text(listing: dict) -> str:
    notes = "; ".join(
        n.get("noteText", "") for n in listing.get("otherExtras") or []
    )
    fields = {
        "Title": listing.get("title"),
        "Year": listing.get("year"),
        "Series/Badge": f'{listing.get("series")} {listing.get("badge") or ""}',
        "Odometer": f'{listing.get("odometer")} ({listing.get("odometerUnit")})',
        "WOVR": listing.get("wovr"),
        "Incident type": ", ".join(listing.get("incidentType") or []),
        "Driveable": listing.get("driveable"),
        "Engine starts": listing.get("engineStarts"),
        "Keys": listing.get("keys"),
        "Burnt": listing.get("burnt"),
        "Description": listing.get("description"),
        "Inspector notes": notes or "none",
    }
    return "\n".join(f"{k}: {v}" for k, v in fields.items())


# ----------------------------------------------------------------------
# Layer 2: Claude vision -> structured damage JSON
# ----------------------------------------------------------------------
def assess_damage(listing: dict) -> dict:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        sys.exit("Set ANTHROPIC_API_KEY.")

    content = fetch_images_b64(listing)
    content.append({
        "type": "text",
        "text": "LISTING DETAILS:\n" + listing_text(listing),
    })

    resp = httpx.post(
        ANTHROPIC_URL,
        timeout=120,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": MODEL,
            "max_tokens": 2000,
            "system": DAMAGE_SCHEMA_INSTRUCTIONS,
            "messages": [{"role": "user", "content": content}],
        },
    )
    resp.raise_for_status()
    text = "".join(
        b["text"] for b in resp.json()["content"] if b["type"] == "text"
    )
    text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return json.loads(text)


# ----------------------------------------------------------------------
# Layer 3 + 4: repair costing and the bid engine (pure math)
# ----------------------------------------------------------------------
def repair_cost(damage: dict, cfg: dict) -> tuple[float, list[str]]:
    c = cfg["repair_costs"]
    total, lines = 0.0, []

    def add(amount: float, label: str):
        nonlocal total
        total += amount
        lines.append(f"  {label:<42} ${amount:>8,.0f}")

    for p in damage.get("panels", []):
        sev = int(p.get("severity", 3))
        if p.get("action") == "replace":
            add(c["panel_replace"] + c["paint_per_panel"],
                f'{p["name"]} (replace+paint, sev {sev})')
        else:
            add(c["panel_repair"] * (0.6 + 0.2 * sev) + c["paint_per_panel"],
                f'{p["name"]} (repair+paint, sev {sev})')

    if damage.get("airbags_deployed") is True:
        add(c["airbag_set"], "airbags deployed (modules+clock spring+ECU)")
    elif damage.get("airbags_deployed") == "unclear":
        add(c["airbag_set"] * 0.5, "airbags UNCLEAR (half provision)")

    for g in damage.get("glass_damaged", []):
        add(c["glass_each"], f"glass: {g}")
    for _ in range(int(damage.get("wheels_damaged", 0))):
        add(c["wheel_each"], "wheel/tyre")
    for l in damage.get("lights_damaged", []):
        add(c["light_each"], f"light: {l}")

    risk = damage.get("drivetrain_impact_risk", "low")
    if risk == "medium":
        add(c["mechanical_provision"], "mechanical provision (medium risk)")
    elif risk == "high":
        add(c["mechanical_provision"] * 2.5, "mechanical provision (HIGH risk)")

    if damage.get("interior_condition") in ("damaged", "stripped"):
        add(c["interior_provision"], "interior repair provision")

    return total, lines


def compute_bid(listing: dict, damage: dict, resale: float, cfg: dict) -> dict:
    e = cfg["economics"]
    wovr = (listing.get("wovr") or "").lower()

    # --- Hard gates -----------------------------------------------------
    if "statutory" in wovr:
        return {"verdict": "PARTS_ONLY",
                "reason": "Statutory Write-Off: can never be re-registered. "
                          "Value = parts/scrap only. Do not price as a flip."}
    if damage.get("structural_damage_suspected"):
        return {"verdict": "INSPECT",
                "reason": "Structural damage suspected from photos. Do not "
                          "bid blind; inspect in person or walk."}
    if damage.get("fire_damage") or damage.get("water_flood_suspected"):
        return {"verdict": "WALK",
                "reason": "Fire or flood indicators: repair economics rarely "
                          "work and resale disclosure obligations apply."}

    # --- Costs ----------------------------------------------------------
    repairs, lines = repair_cost(damage, cfg)

    # Risk buffer: base + uncertainty flags + model's hidden-damage estimate
    buffer = e["risk_buffer_base"]
    flags = []
    if listing.get("engineStarts") is False:
        buffer += e["flag_engine_no_start"]; flags.append("engine doesn't start")
    if listing.get("keys") is False:
        buffer += e["flag_no_keys"]; flags.append("no keys")
    if listing.get("odometer") is None:
        buffer += e["flag_odometer_unknown"]; flags.append("odometer unknown")
    if listing.get("driveable") is False:
        buffer += e["flag_not_driveable"]; flags.append("not driveable")
    buffer += damage.get("hidden_damage_risk", 0.3) * e["hidden_risk_scale"]

    # WOVR repairable path costs (inspection + re-rego), only if on register
    wovr_cost = e["wovr_inspection_rego"] if "repairable" in wovr else 0.0

    fixed = repairs + e["transport"] + wovr_cost + buffer
    target_net = resale * (1 - e["target_margin"])

    # Solve: bid*(1+fee_pct) + fee_fixed + fixed = target_net
    max_bid = (target_net - e["buyer_fee_fixed"] - fixed) / (1 + e["buyer_fee_pct"])
    max_bid = max(0.0, max_bid)

    verdict = "BID" if max_bid > e["min_worthwhile_bid"] else "WALK"
    return {
        "verdict": verdict,
        "max_bid": round(max_bid, -1),
        "expected_resale": resale,
        "target_margin": e["target_margin"],
        "repair_cost": repairs,
        "repair_lines": lines,
        "transport": e["transport"],
        "wovr_costs": wovr_cost,
        "risk_buffer": buffer,
        "risk_flags": flags,
        "buyer_fees_at_max_bid": round(max_bid * e["buyer_fee_pct"]
                                       + e["buyer_fee_fixed"]),
    }


# ----------------------------------------------------------------------
# Public entry point: run the full pipeline and persist the result.
# Used by both the CLI below and dashboard.py's "Appraise this car" button.
# ----------------------------------------------------------------------
def run_appraisal(listing: dict, resale: float, cfg: dict | None = None) -> tuple[dict, dict]:
    """Assess damage from photos, then compute the bid. Returns (damage, result)."""
    cfg = cfg or json.loads(CONFIG_FILE.read_text())
    damage = assess_damage(listing)
    result = compute_bid(listing, damage, resale, cfg)
    return damage, result


def save_appraisal(stock_number: str, resale: float, damage: dict, result: dict) -> None:
    import psycopg
    with psycopg.connect(DATABASE_URL) as conn:
        conn.execute(
            """INSERT INTO appraisals
                 (stock_number, resale_input, verdict, max_bid, repair_cost,
                  risk_buffer, damage, result)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                stock_number, resale, result.get("verdict"), result.get("max_bid"),
                result.get("repair_cost"), result.get("risk_buffer"),
                json.dumps(damage, default=str), json.dumps(result, default=str),
            ),
        )
        conn.commit()


# ----------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stock", help="stock number (reads from collector DB)")
    ap.add_argument("--json", help="path to raw listing JSON instead")
    ap.add_argument("--resale", type=float, required=True,
                    help="your repaired-resale estimate in AUD")
    args = ap.parse_args()

    listing = (json.loads(Path(args.json).read_text()) if args.json
               else load_listing_from_db(args.stock))

    print(f"\n=== {listing.get('title')} {listing.get('series') or ''} "
          f"{listing.get('badge') or ''} ({listing.get('year')}) ===")
    print(f"WOVR: {listing.get('wovr')} | Odo: {listing.get('odometer')} | "
          f"Location: {(listing.get('productLocation') or {}).get('suburb')}")

    print("\nAssessing damage from photos (Claude vision)...")
    damage, result = run_appraisal(listing, args.resale)
    print(f"Assessment: {damage.get('summary')}")
    print(f"Overall severity {damage.get('overall_severity')}/5 | "
          f"airbags: {damage.get('airbags_deployed')} | "
          f"structural: {damage.get('structural_damage_suspected')} | "
          f"hidden risk: {damage.get('hidden_damage_risk')}")
    if damage.get("description_photo_mismatch") not in (None, "none"):
        print(f"!! Photo/description mismatch: "
              f"{damage['description_photo_mismatch']}")

    print(f"\nVERDICT: {result['verdict']}")
    if result["verdict"] in ("PARTS_ONLY", "INSPECT", "WALK") and "reason" in result:
        print(result["reason"])
    if "max_bid" in result:
        print(f"\nMAX BID: ${result['max_bid']:,.0f}")
        print(f"  Expected resale                          "
              f"${result['expected_resale']:>8,.0f}")
        print(f"  Target margin                            "
              f"{result['target_margin']*100:>7.0f}%")
        print("  Repair estimate:")
        for line in result["repair_lines"]:
            print(line)
        print(f"  {'Repairs total':<42} ${result['repair_cost']:>8,.0f}")
        print(f"  {'Transport':<42} ${result['transport']:>8,.0f}")
        print(f"  {'WOVR inspection + rego':<42} ${result['wovr_costs']:>8,.0f}")
        print(f"  {'Risk buffer (' + ', '.join(result['risk_flags'] or ['base']) + ')':<42} "
              f"${result['risk_buffer']:>8,.0f}")
        print(f"  {'Buyer fees at max bid (approx)':<42} "
              f"${result['buyer_fees_at_max_bid']:>8,.0f}")

    # Persist the appraisal for future calibration against actual sale_results
    stock = str(listing.get("stockNumber") or listing.get("id"))
    save_appraisal(stock, args.resale, damage, result)
    print(f"\nSaved appraisal for {stock} to the database "
          f"(compare against sale_results later to calibrate bid_config.json).")


if __name__ == "__main__":
    main()
