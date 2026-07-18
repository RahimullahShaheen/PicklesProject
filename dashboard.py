"""
Interactive browser for the listings collected by collector.py.

Usage:
    export DATABASE_URL=postgresql://localhost/pickles
    streamlit run dashboard.py
"""

from __future__ import annotations

import base64
import os
from pathlib import Path

import pandas as pd
import psycopg
import streamlit as st

from appraiser import run_appraisal, save_appraisal

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://localhost/pickles")
BASE_DIR = Path(__file__).parent

st.set_page_config(page_title="Pickles Salvage Listings", layout="wide")


def resolve_local_image(local_path: str | None) -> Path | None:
    """Return the on-disk path for an image if download_images.py has fetched it."""
    if not local_path:
        return None
    path = Path(local_path)
    if not path.is_absolute():
        path = BASE_DIR / path
    return path if path.exists() else None


def image_src(local_path: str | None, cdn_url: str) -> tuple[str, bool]:
    """Return (src, is_local) — a data: URI for local files, else the CDN URL."""
    local = resolve_local_image(local_path)
    if not local:
        return cdn_url, False
    mime = "image/png" if local.suffix.lower() == ".png" else "image/jpeg"
    data = base64.b64encode(local.read_bytes()).decode()
    return f"data:{mime};base64,{data}", True


def render_image_carousel(stock_number: str, images: pd.DataFrame) -> None:
    """A single square photo with prev/next slide buttons."""
    if images.empty:
        st.write("No images recorded.")
        return

    idx_key = f"img_idx_{stock_number}"
    idx = st.session_state.get(idx_key, 0) % len(images)
    img = images.iloc[idx]
    src, is_local = image_src(img["local_path"], img["cdn_url"])

    st.markdown(
        f"""<div style="width:100%;aspect-ratio:1/1;overflow:hidden;
                border-radius:10px;background:#111;">
              <img src="{src}" style="width:100%;height:100%;object-fit:cover;" />
            </div>""",
        unsafe_allow_html=True,
    )

    prev_col, counter_col, next_col = st.columns([1, 2, 1])
    if prev_col.button("◀", key=f"prev_{stock_number}", use_container_width=True):
        st.session_state[idx_key] = (idx - 1) % len(images)
        st.rerun()
    counter_col.markdown(
        f"<div style='text-align:center;padding-top:0.4rem;'>{idx + 1} / {len(images)}</div>",
        unsafe_allow_html=True,
    )
    if next_col.button("▶", key=f"next_{stock_number}", use_container_width=True):
        st.session_state[idx_key] = (idx + 1) % len(images)
        st.rerun()

    if not is_local:
        st.caption(
            "Shown from Pickles' CDN — not downloaded locally. "
            "Run download_images.py to fetch it."
        )


def go_to_list() -> None:
    st.query_params.clear()
    st.session_state.pop("confirm_delete", None)
    st.rerun()


def go_to_detail(stock_number: str) -> None:
    st.query_params["stock"] = stock_number
    st.rerun()


@st.cache_data(ttl=60)
def load_listings() -> pd.DataFrame:
    query = """
        SELECT l.stock_number, l.title, l.make, l.model, l.series, l.badge,
               l.year, l.body, l.colour, l.odometer, l.odometer_unit,
               l.transmission, l.fuel_type,
               l.wovr, l.incident_types, l.driveable, l.engine_starts,
               l.has_keys, l.burnt,
               l.redbook_code, l.state, l.city, l.suburb,
               l.buy_method, l.sale_name, l.sale_end_utc, l.product_bid_end_utc,
               l.first_seen_at, l.last_seen_at, l.disappeared_at,
               s.minimum_bid, s.highest_bid, s.buy_now_price, s.price,
               s.sale_status, s.for_sale, s.captured_at AS snapshot_at
        FROM listings l
        LEFT JOIN LATERAL (
            SELECT * FROM snapshots sn
            WHERE sn.stock_number = l.stock_number
            ORDER BY captured_at DESC
            LIMIT 1
        ) s ON true
        ORDER BY l.last_seen_at DESC
    """
    with psycopg.connect(DATABASE_URL) as conn:
        return pd.read_sql(query, conn)


@st.cache_data(ttl=60)
def load_images(stock_number: str) -> pd.DataFrame:
    with psycopg.connect(DATABASE_URL) as conn:
        return pd.read_sql(
            "SELECT cdn_url, sequence, local_path FROM images "
            "WHERE stock_number=%(sn)s ORDER BY sequence",
            conn, params={"sn": stock_number},
        )


@st.cache_data(ttl=60)
def load_price_history(stock_number: str) -> pd.DataFrame:
    with psycopg.connect(DATABASE_URL) as conn:
        return pd.read_sql(
            "SELECT captured_at, minimum_bid, highest_bid, price FROM snapshots "
            "WHERE stock_number=%(sn)s ORDER BY captured_at",
            conn, params={"sn": stock_number},
        )


def load_raw_listing(stock_number: str) -> dict:
    with psycopg.connect(DATABASE_URL) as conn:
        row = conn.execute(
            "SELECT raw FROM listings WHERE stock_number = %s", (stock_number,)
        ).fetchone()
    return row[0]


def load_appraisals(stock_number: str) -> pd.DataFrame:
    with psycopg.connect(DATABASE_URL) as conn:
        return pd.read_sql(
            "SELECT created_at, resale_input, verdict, max_bid, repair_cost, risk_buffer "
            "FROM appraisals WHERE stock_number=%(sn)s ORDER BY created_at DESC",
            conn, params={"sn": stock_number},
        )


@st.cache_data(ttl=60)
def load_shortlist() -> pd.DataFrame:
    """Tier 1 ranking: every active, scored listing, least visible damage first."""
    query = """
        SELECT l.stock_number, l.title, l.make, l.model, l.year, l.wovr,
               l.state, l.suburb, l.odometer, l.product_bid_end_utc,
               d.images_scanned, d.detection_count, d.area_score, d.class_counts,
               d.scored_at
        FROM damage_scores d
        JOIN listings l ON l.stock_number = d.stock_number
        WHERE l.disappeared_at IS NULL
        ORDER BY d.area_score ASC
    """
    with psycopg.connect(DATABASE_URL) as conn:
        return pd.read_sql(query, conn)


@st.cache_data(ttl=60)
def scored_vs_total() -> tuple[int, int]:
    with psycopg.connect(DATABASE_URL) as conn:
        scored = conn.execute(
            "SELECT count(*) FROM damage_scores d JOIN listings l "
            "ON l.stock_number = d.stock_number WHERE l.disappeared_at IS NULL"
        ).fetchone()[0]
        total = conn.execute(
            "SELECT count(*) FROM listings WHERE disappeared_at IS NULL"
        ).fetchone()[0]
    return scored, total


def delete_listing(stock_number: str) -> None:
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM appraisals WHERE stock_number=%s", (stock_number,))
            cur.execute("DELETE FROM images WHERE stock_number=%s", (stock_number,))
            cur.execute("DELETE FROM snapshots WHERE stock_number=%s", (stock_number,))
            cur.execute("DELETE FROM sale_results WHERE stock_number=%s", (stock_number,))
            cur.execute("DELETE FROM listings WHERE stock_number=%s", (stock_number,))
        conn.commit()
    load_listings.clear()


def render_nav() -> None:
    """List/Shortlist switcher — shown in the sidebar on both non-detail pages."""
    nav_col1, nav_col2 = st.sidebar.columns(2)
    if nav_col1.button("📋 All Listings", use_container_width=True):
        st.query_params.clear()
        st.rerun()
    if nav_col2.button("🎯 Shortlist", use_container_width=True):
        st.query_params.clear()
        st.query_params["view"] = "shortlist"
        st.rerun()
    st.sidebar.divider()


# ----------------------------------------------------------------------
# List page
# ----------------------------------------------------------------------
def render_list_page() -> None:
    df = load_listings()

    render_nav()
    st.title("Pickles Salvage Listings")

    if df.empty:
        st.warning("No listings in the database yet. Run collector.py first.")
        st.stop()

    active = df[df["disappeared_at"].isna()]
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total listings ever seen", len(df))
    c2.metric("Currently active", len(active))
    c3.metric("Disappeared / sold", len(df) - len(active))
    c4.metric("Last collected", df["last_seen_at"].max().strftime("%Y-%m-%d %H:%M"))

    st.sidebar.header("Filters")
    show_active_only = st.sidebar.checkbox("Active listings only", value=True)
    makes = st.sidebar.multiselect("Make", sorted(df["make"].dropna().unique()))
    wovr = st.sidebar.multiselect("WOVR status", sorted(df["wovr"].dropna().unique()))
    states = st.sidebar.multiselect("State", sorted(df["state"].dropna().unique()))
    search = st.sidebar.text_input("Search title")

    years = df["year"].dropna()
    if len(years):
        year_min, year_max = int(years.min()), int(years.max())
        year_range = st.sidebar.slider("Year", year_min, year_max, (year_min, year_max))
    else:
        year_range = None

    filtered = active if show_active_only else df
    if makes:
        filtered = filtered[filtered["make"].isin(makes)]
    if wovr:
        filtered = filtered[filtered["wovr"].isin(wovr)]
    if states:
        filtered = filtered[filtered["state"].isin(states)]
    if search:
        filtered = filtered[filtered["title"].str.contains(search, case=False, na=False)]
    if year_range:
        filtered = filtered[filtered["year"].between(*year_range) | filtered["year"].isna()]

    st.write(f"**{len(filtered)}** listings match filters — click a row to open it")

    display_cols = ["stock_number", "title", "year", "wovr", "state", "suburb",
                     "odometer", "highest_bid", "minimum_bid", "sale_status",
                     "product_bid_end_utc"]
    event = st.dataframe(
        filtered[display_cols],
        use_container_width=True,
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
    )

    selected_rows = event.selection.rows if event and event.selection else []
    if selected_rows:
        stock_number = filtered.iloc[selected_rows[0]]["stock_number"]
        go_to_detail(stock_number)


# ----------------------------------------------------------------------
# Shortlist page (Tier 1: local YOLO damage sieve)
# ----------------------------------------------------------------------
def render_shortlist_page() -> None:
    render_nav()
    st.title("🎯 Shortlist — Tier 1 (local damage scan)")
    st.caption(
        "Ranked least-damaged first, from score_damage.py's local YOLOv8s scan "
        "(no API cost). Use this to pick which cars are worth a Tier 2 Claude "
        "appraisal, instead of browsing every listing."
    )

    scored, total = scored_vs_total()
    st.write(f"**{scored} / {total}** active listings scored.")
    if scored < total:
        st.info(
            f"{total - scored} active listing(s) not scanned yet. Run "
            f"`python score_damage.py` to score them (or `--rescan` to redo all)."
        )
    if scored == 0:
        return

    shortlist_all = load_shortlist()

    st.sidebar.header("Shortlist filters")
    makes = st.sidebar.multiselect(
        "Make", sorted(shortlist_all["make"].dropna().unique()), key="sl_make"
    )
    filtered = shortlist_all[shortlist_all["make"].isin(makes)] if makes else shortlist_all

    # Model options depend on the Make filter — drop stale selections that no
    # longer apply (e.g. switching Make away from a previously-picked model)
    # before instantiating the widget, so Streamlit doesn't choke on a
    # selected value outside the new options list.
    valid_models = sorted(filtered["model"].dropna().unique())
    if st.session_state.get("sl_model"):
        st.session_state["sl_model"] = [
            m for m in st.session_state["sl_model"] if m in valid_models
        ]
    models = st.sidebar.multiselect("Model", valid_models, key="sl_model")
    if models:
        filtered = filtered[filtered["model"].isin(models)]

    wovr = st.sidebar.multiselect(
        "WOVR status", sorted(shortlist_all["wovr"].dropna().unique()), key="sl_wovr"
    )
    states = st.sidebar.multiselect(
        "State", sorted(shortlist_all["state"].dropna().unique()), key="sl_state"
    )
    search = st.sidebar.text_input("Search title", key="sl_search")

    if wovr:
        filtered = filtered[filtered["wovr"].isin(wovr)]
    if states:
        filtered = filtered[filtered["state"].isin(states)]
    if search:
        filtered = filtered[filtered["title"].str.contains(search, case=False, na=False)]

    limit = st.slider("Shortlist size", min_value=5, max_value=50, value=30, step=5)
    shortlist = filtered.head(limit)

    st.write(
        f"**{len(filtered)}** scored listing(s) match your filters — "
        f"showing the {len(shortlist)} least-damaged."
    )

    display_cols = ["stock_number", "title", "year", "wovr", "state", "suburb",
                     "odometer", "detection_count", "area_score",
                     "images_scanned", "product_bid_end_utc"]
    event = st.dataframe(
        shortlist[display_cols].rename(columns={
            "detection_count": "damage detections",
            "area_score": "damage area score (lower = cleaner)",
            "images_scanned": "photos scanned",
        }),
        use_container_width=True,
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
    )

    selected_rows = event.selection.rows if event and event.selection else []
    if selected_rows:
        stock_number = shortlist.iloc[selected_rows[0]]["stock_number"]
        go_to_detail(stock_number)


# ----------------------------------------------------------------------
# Detail page
# ----------------------------------------------------------------------
def render_detail_page(stock_number: str) -> None:
    df = load_listings()
    matches = df[df["stock_number"] == stock_number]

    st.sidebar.header("Navigation")
    if st.sidebar.button("⬅ Back to list", key="sidebar_back", use_container_width=True):
        go_to_list()
    st.sidebar.write(f"Stock number: **{stock_number}**")

    if matches.empty:
        st.error(f"Stock number {stock_number} not found (it may have been deleted).")
        if st.button("⬅ Back to list"):
            go_to_list()
        return
    row = matches.iloc[0]

    col_back, col_delete, _ = st.columns([1, 1, 4])
    with col_back:
        if st.button("⬅ Back to list"):
            go_to_list()
    with col_delete:
        if not st.session_state.get("confirm_delete"):
            if st.button("🗑 Delete listing"):
                st.session_state.confirm_delete = True
                st.rerun()

    if st.session_state.get("confirm_delete"):
        st.warning(
            "Permanently delete this listing and all its snapshots, images, "
            "and appraisals? This cannot be undone."
        )
        yes_col, no_col, _ = st.columns([1, 1, 4])
        if yes_col.button("Yes, delete permanently"):
            delete_listing(stock_number)
            go_to_list()
        if no_col.button("Cancel"):
            st.session_state.confirm_delete = False
            st.rerun()
        return

    st.title(row["title"] or stock_number)
    left, right = st.columns([2, 1])

    with left:
        st.subheader("Details")
        detail_fields = {
            "Stock number": row["stock_number"],
            "Make / Model": f"{row['make']} {row['model']}",
            "Series / Badge": f"{row['series']} / {row['badge']}",
            "Year": row["year"],
            "Body": row["body"],
            "Colour": row["colour"],
            "Odometer": f"{row['odometer']} {row['odometer_unit']}",
            "Transmission": row["transmission"],
            "Fuel type": row["fuel_type"],
            "WOVR": row["wovr"],
            "Incident types": row["incident_types"],
            "Driveable": row["driveable"],
            "Engine starts": row["engine_starts"],
            "Has keys": row["has_keys"],
            "Burnt": row["burnt"],
            "RedBook code": row["redbook_code"],
            "Location": f"{row['suburb']}, {row['state']}",
            "Buy method": row["buy_method"],
            "Sale": row["sale_name"],
            "Sale ends": row["product_bid_end_utc"],
            "Minimum bid": row["minimum_bid"],
            "Highest bid": row["highest_bid"],
            "Buy now price": row["buy_now_price"],
            "Sale status": row["sale_status"],
            "First seen": row["first_seen_at"],
            "Last seen": row["last_seen_at"],
            "Disappeared at": row["disappeared_at"],
        }
        st.table(pd.DataFrame(
            [(k, str(v)) for k, v in detail_fields.items()],
            columns=["Field", "Value"],
        ))

    with right:
        st.subheader("Images")
        render_image_carousel(stock_number, load_images(stock_number))

    st.subheader("Price history")
    history = load_price_history(stock_number)
    if len(history) > 1:
        st.line_chart(history.set_index("captured_at")[["minimum_bid", "highest_bid", "price"]])
    else:
        st.write("Not enough snapshot history yet (needs more than one collector run).")

    st.subheader("Appraisal")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        st.warning(
            "ANTHROPIC_API_KEY is not set in this session — set it and restart "
            "the dashboard to run appraisals."
        )
    else:
        resale = st.number_input(
            "Your repaired-resale estimate (AUD, from RedBook/carsales)",
            min_value=0, step=500, key=f"resale_{stock_number}",
        )
        if st.button("Appraise this car (runs appraiser.py)",
                      key=f"appraise_{stock_number}", disabled=resale <= 0):
            with st.spinner("Sending photos to Claude for damage assessment..."):
                raw_listing = load_raw_listing(stock_number)
                damage, result = run_appraisal(raw_listing, resale)
                save_appraisal(stock_number, resale, damage, result)
            st.success(f"Verdict: {result['verdict']}")
            st.write(damage.get("summary"))
            if result["verdict"] in ("PARTS_ONLY", "INSPECT", "WALK") and "reason" in result:
                st.write(result["reason"])
            if "max_bid" in result:
                st.metric("Max bid", f"${result['max_bid']:,.0f}")
                st.text("\n".join(result["repair_lines"]))
                st.write(
                    f"Repairs: ${result['repair_cost']:,.0f} · "
                    f"Transport: ${result['transport']:,.0f} · "
                    f"WOVR costs: ${result['wovr_costs']:,.0f} · "
                    f"Risk buffer: ${result['risk_buffer']:,.0f} "
                    f"({', '.join(result['risk_flags'] or ['base'])})"
                )

    past = load_appraisals(stock_number)
    if len(past):
        st.write("Past appraisals for this car:")
        st.dataframe(past, use_container_width=True, hide_index=True)


# ----------------------------------------------------------------------
st.sidebar.title("🚗 Pickles Salvage")

selected_stock = st.query_params.get("stock")
if selected_stock:
    render_detail_page(selected_stock)
elif st.query_params.get("view") == "shortlist":
    render_shortlist_page()
else:
    render_list_page()
