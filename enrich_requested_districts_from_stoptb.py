from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import time
import re
from pathlib import Path

import geopandas as gpd
import pandas as pd
import requests

from archive_utils import archive_existing_path, archive_existing_tree
from connectivity_pipeline.pipeline import load_config, run_pipeline


ROOT = Path(__file__).resolve().parent
SOURCE_CSV = Path(r"E:\Resources\SecondBrain\outputs\stoptb_ben_screen_30_Jun_2026_mapped.csv")
REQUESTED_DISTRICTS_CSV = Path(r"E:\Resources\SecondBrain\outputs\phi_requested_districts_20260629\phi_requested_districts.csv")
BASE_CONFIG = ROOT / "config.yaml"
GLOBAL_OOKLA_PARQUET = ROOT / "data" / "ookla_mobile_tiles_q1_2026.parquet"
DISTRICT_OUTPUT_ROOT = ROOT / "outputs" / "requested_districts"
STATUS_CSV = DISTRICT_OUTPUT_ROOT / "requested_district_status.csv"
SITE_ROOT = ROOT / "outputs" / "requested_districts_site"
SHARED_TOWER_POOL = DISTRICT_OUTPUT_ROOT / "data" / "shared_opencellid_pool.csv.gz"
STATE_CACHE_ROOT = DISTRICT_OUTPUT_ROOT / "_state_cache"
LGD_CACHE_ROOT = ROOT / "data" / "cache" / "lgd"
LGD_VILLAGES_CSV = LGD_CACHE_ROOT / "lgd_villages.csv"
GEOCODE_CACHE_CSV = LGD_CACHE_ROOT / "nominatim_block_centroids.csv"
LGD_VILLAGES_URL = "https://www.data.gov.in/files/ogdpv2dms/s3fs-public/datafile/lgd_villages.csv"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
GEOCODE_USER_AGENT = "koraput-connectivity-pipeline/1.0"
GEOCODE_SLEEP_SECONDS = 1.1
MAX_BLOCK_GEOCODES_PER_DISTRICT = 80

DISTRICT_ALIASES = {
    "alluri sitharama raju": "Alluri Sitarama Raju",
    "alluri sita ramaraju": "Alluri Sitarama Raju",
    "khargone (west nimar)": "Khargone",
    "khargone west nimar": "Khargone",
    "parvathi puram": "Parvathipuram Manyam",
    "parvathi puram manyam": "Parvathipuram Manyam",
    "parvathipuram": "Parvathipuram Manyam",
    "parvathipuram manyam": "Parvathipuram Manyam",
}

_LGD_MASTER_CACHE: pd.DataFrame | None = None


def normalize_text(value: object) -> str:
    return " ".join(str(value or "").strip().lower().replace("&", " and ").split())


def slugify(value: str) -> str:
    return "".join(ch.lower() if ch.isalnum() else "_" for ch in value).strip("_")


def canonicalize_district(value: object) -> str:
    normalized = normalize_text(value)
    return DISTRICT_ALIASES.get(normalized, str(value or "").strip())


def canonicalize_state(value: object) -> str:
    text = str(value or "").strip()
    return " ".join(part.capitalize() for part in text.split())


def district_key(state: str, district: str) -> tuple[str, str]:
    return (normalize_text(state), normalize_text(district))


def load_requested_districts() -> list[dict[str, str]]:
    requested_df = pd.read_csv(REQUESTED_DISTRICTS_CSV, low_memory=False)
    required = ["requested_state", "requested_district", "matched_state", "matched_district"]
    missing = [column for column in required if column not in requested_df.columns]
    if missing:
        raise ValueError(f"Missing required columns in requested district list: {missing}")

    unique_rows = (
        requested_df[required]
        .dropna(subset=["requested_state", "requested_district", "matched_state", "matched_district"])
        .drop_duplicates()
        .copy()
    )

    requested_districts: list[dict[str, str]] = []
    for row in unique_rows.itertuples(index=False):
        requested_state = canonicalize_state(row.requested_state)
        requested_district = canonicalize_district(row.requested_district)
        matched_state = canonicalize_state(row.matched_state)
        matched_district = canonicalize_district(row.matched_district)
        requested_districts.append(
            {
                "requested_state": requested_state,
                "requested_district": requested_district,
                "matched_state": matched_state,
                "matched_district": matched_district,
            }
        )
    requested_districts.sort(key=lambda item: (item["requested_state"], item["requested_district"]))
    return requested_districts


def load_requested_facility_rows() -> pd.DataFrame:
    return pd.read_csv(REQUESTED_DISTRICTS_CSV, low_memory=False)


def ensure_lgd_village_master() -> Path:
    if LGD_VILLAGES_CSV.exists():
        return LGD_VILLAGES_CSV

    LGD_CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    response = requests.get(
        LGD_VILLAGES_URL,
        headers={"User-Agent": GEOCODE_USER_AGENT},
        timeout=300,
        stream=True,
    )
    response.raise_for_status()
    tmp_path = LGD_VILLAGES_CSV.with_suffix(".tmp")
    with tmp_path.open("wb") as handle:
        for chunk in response.iter_content(chunk_size=1024 * 1024):
            if chunk:
                handle.write(chunk)
    tmp_path.replace(LGD_VILLAGES_CSV)
    return LGD_VILLAGES_CSV


def load_lgd_village_master() -> pd.DataFrame:
    global _LGD_MASTER_CACHE
    if _LGD_MASTER_CACHE is not None:
        return _LGD_MASTER_CACHE

    csv_path = ensure_lgd_village_master()
    usecols = [
        "VillageCode",
        "VillageNameEnglish",
        "SubDistrictNameEnglish",
        "DistrictNameEnglish",
        "StateNameEnglish",
    ]
    master = pd.read_csv(csv_path, usecols=usecols, low_memory=False)
    master = master.rename(
        columns={
            "VillageCode": "lgd_code",
            "VillageNameEnglish": "village",
            "SubDistrictNameEnglish": "block",
            "DistrictNameEnglish": "district",
            "StateNameEnglish": "state",
        }
    )
    master["state"] = master["state"].map(canonicalize_state)
    master["district"] = master["district"].map(canonicalize_district)
    master["block"] = master["block"].fillna("").astype(str).str.strip()
    master["village"] = master["village"].fillna("").astype(str).str.strip()
    master["lgd_code"] = master["lgd_code"].astype(str).str.strip()
    master = master[(master["block"] != "") & (master["village"] != "")]
    _LGD_MASTER_CACHE = master
    return _LGD_MASTER_CACHE


def load_geocode_cache() -> pd.DataFrame:
    if not GEOCODE_CACHE_CSV.exists():
        return pd.DataFrame(columns=["query", "latitude", "longitude", "source"])
    return pd.read_csv(GEOCODE_CACHE_CSV)


def persist_geocode_cache(cache_df: pd.DataFrame) -> None:
    GEOCODE_CACHE_CSV.parent.mkdir(parents=True, exist_ok=True)
    archive_existing_path(GEOCODE_CACHE_CSV)
    cache_df.drop_duplicates(subset=["query"], keep="last").to_csv(GEOCODE_CACHE_CSV, index=False)


def geocode_place(query: str, cache_df: pd.DataFrame) -> tuple[float | None, float | None, pd.DataFrame]:
    existing = cache_df.loc[cache_df["query"] == query]
    if not existing.empty:
        row = existing.iloc[-1]
        return pd.to_numeric(row["latitude"], errors="coerce"), pd.to_numeric(row["longitude"], errors="coerce"), cache_df

    response = requests.get(
        NOMINATIM_URL,
        params={"q": query, "format": "jsonv2", "limit": 1},
        headers={"User-Agent": GEOCODE_USER_AGENT},
        timeout=60,
    )
    response.raise_for_status()
    payload = response.json()
    lat: float | None = None
    lon: float | None = None
    if payload:
        lat = float(payload[0]["lat"])
        lon = float(payload[0]["lon"])

    cache_df = pd.concat(
        [
            cache_df,
            pd.DataFrame(
                [
                    {
                        "query": query,
                        "latitude": lat,
                        "longitude": lon,
                        "source": "nominatim",
                    }
                ]
            ),
        ],
        ignore_index=True,
    )
    time.sleep(GEOCODE_SLEEP_SECONDS)
    return lat, lon, cache_df


def build_lgd_fallback_villages(
    requested_item: dict[str, str],
    requested_facility_df: pd.DataFrame,
) -> pd.DataFrame:
    facility_rows = requested_facility_df[
        (requested_facility_df["requested_state"].map(canonicalize_state).map(normalize_text) == normalize_text(requested_item["requested_state"]))
        & (requested_facility_df["requested_district"].astype(str).str.strip().map(normalize_text) == normalize_text(requested_item["requested_district"]))
    ].copy()
    facility_rows["tb_unit_norm"] = facility_rows["tb_unit"].fillna("").astype(str).str.strip().map(normalize_text)
    block_norms = {value for value in facility_rows["tb_unit_norm"].tolist() if value}

    try:
        lgd_master = load_lgd_village_master()
        district_rows = lgd_master[
            (lgd_master["state"].map(normalize_text) == normalize_text(requested_item["matched_state"]))
            & (lgd_master["district"].map(normalize_text) == normalize_text(requested_item["matched_district"]))
        ].copy()
        if not district_rows.empty and block_norms:
            matched_blocks = district_rows["block"].map(normalize_text).isin(block_norms)
            if matched_blocks.any():
                district_rows = district_rows[matched_blocks].copy()
        if not district_rows.empty:
            district_rows = district_rows.drop_duplicates(subset=["state", "district", "block", "village", "lgd_code"]).copy()
            district_rows["latitude"] = pd.NA
            district_rows["longitude"] = pd.NA
            return assign_fallback_block_centroids(district_rows)
    except requests.RequestException:
        pass

    proxy_rows = pd.DataFrame(
        [
            {
                "state": requested_item["matched_state"],
                "district": requested_item["matched_district"],
                "block": "District proxy",
                "village": f"{requested_item['requested_district']} proxy anchor",
                "lgd_code": "",
                "coordinate_source": "district_proxy",
            }
        ]
    )
    return assign_district_proxy_centroid(proxy_rows)


def assign_district_proxy_centroid(villages_df: pd.DataFrame) -> pd.DataFrame:
    villages = villages_df.copy()
    if villages.empty:
        return villages

    state = str(villages["state"].iloc[0])
    district = str(villages["district"].iloc[0])
    cache_df = load_geocode_cache()
    district_query = f"{district}, {state}, India"
    district_lat, district_lon, cache_df = geocode_place(district_query, cache_df)
    persist_geocode_cache(cache_df)
    villages["latitude"] = district_lat
    villages["longitude"] = district_lon
    villages["latitude"] = pd.to_numeric(villages["latitude"], errors="coerce")
    villages["longitude"] = pd.to_numeric(villages["longitude"], errors="coerce")
    if "coordinate_source" not in villages.columns:
        villages["coordinate_source"] = "district_proxy"
    return villages


def assign_fallback_block_centroids(villages_df: pd.DataFrame) -> pd.DataFrame:
    villages = villages_df.copy()
    if villages.empty:
        return villages

    state = str(villages["state"].iloc[0])
    district = str(villages["district"].iloc[0])
    cache_df = load_geocode_cache()
    block_values = villages["block"].fillna("").astype(str).str.strip()
    unique_blocks = sorted({value for value in block_values.tolist() if value})
    if len(unique_blocks) > MAX_BLOCK_GEOCODES_PER_DISTRICT:
        unique_blocks = unique_blocks[:MAX_BLOCK_GEOCODES_PER_DISTRICT]

    block_centroids: dict[str, tuple[float | None, float | None]] = {}
    for block in unique_blocks:
        query = f"{block}, {district}, {state}, India"
        lat, lon, cache_df = geocode_place(query, cache_df)
        block_centroids[block] = (lat, lon)

    district_query = f"{district}, {state}, India"
    district_lat, district_lon, cache_df = geocode_place(district_query, cache_df)
    persist_geocode_cache(cache_df)

    villages["latitude"] = villages["block"].map(lambda value: block_centroids.get(str(value).strip(), (None, None))[0])
    villages["longitude"] = villages["block"].map(lambda value: block_centroids.get(str(value).strip(), (None, None))[1])
    villages["latitude"] = pd.to_numeric(villages["latitude"], errors="coerce")
    villages["longitude"] = pd.to_numeric(villages["longitude"], errors="coerce")
    unresolved = villages["latitude"].isna() | villages["longitude"].isna()
    if unresolved.any():
        villages.loc[unresolved, "latitude"] = district_lat
        villages.loc[unresolved, "longitude"] = district_lon
    return villages


def requested_matches_filter(item: dict[str, str], requested_state: str, requested_district: str) -> bool:
    if requested_state and normalize_text(item["requested_state"]) != normalize_text(requested_state):
        return False
    if requested_district and normalize_text(item["requested_district"]) != normalize_text(requested_district):
        return False
    return True


def ensure_ookla_subset(villages: pd.DataFrame, output_geojson: Path) -> None:
    min_lat = float(villages["latitude"].min()) - 0.15
    max_lat = float(villages["latitude"].max()) + 0.15
    min_lon = float(villages["longitude"].min()) - 0.15
    max_lon = float(villages["longitude"].max()) + 0.15

    parquet = pd.read_parquet(
        GLOBAL_OOKLA_PARQUET,
        columns=["tile", "tile_x", "tile_y", "avg_d_kbps", "avg_u_kbps", "tests", "devices"],
    )
    subset = parquet[
        parquet["tile_x"].between(min_lon, max_lon) & parquet["tile_y"].between(min_lat, max_lat)
    ].copy()
    subset["avg_download_mbps"] = subset["avg_d_kbps"] / 1000.0
    subset["avg_upload_mbps"] = subset["avg_u_kbps"] / 1000.0

    subset["geometry"] = gpd.GeoSeries.from_wkt(subset["tile"])
    gdf = gpd.GeoDataFrame(
        subset[["avg_download_mbps", "avg_upload_mbps", "tests", "devices", "geometry"]],
        geometry="geometry",
        crs="EPSG:4326",
    )
    output_geojson.parent.mkdir(parents=True, exist_ok=True)
    gdf.to_file(output_geojson, driver="GeoJSON")


def build_village_master(df: pd.DataFrame) -> pd.DataFrame:
    work = df.copy()
    work["state"] = work["login_info-state_name"].map(canonicalize_state)
    work["district"] = work["login_info-district_name"].map(canonicalize_district)
    work["block"] = work["beneficiary_info-block"].fillna("").astype(str).str.strip()
    work["village"] = work["beneficiary_info-village"].fillna("").astype(str).str.strip()
    work["latitude"] = pd.to_numeric(work["auto_gps-Latitude"], errors="coerce")
    work["longitude"] = pd.to_numeric(work["auto_gps-Longitude"], errors="coerce")
    work = work[(work["block"] != "") & (work["village"] != "")]
    work["lgd_code"] = pd.NA

    grouped = (
        work.groupby(["state", "district", "block", "village"], dropna=False)
        .agg(
            latitude=("latitude", "median"),
            longitude=("longitude", "median"),
            source_rows=("deviceid", "size"),
            gps_rows=("latitude", lambda s: int(s.notna().sum())),
        )
        .reset_index()
    )
    grouped["lgd_code"] = grouped.index + 1
    return grouped[["state", "district", "block", "village", "lgd_code", "latitude", "longitude", "source_rows", "gps_rows"]]


def build_shared_tower_pool(exclude_dirs: set[Path] | None = None) -> Path | None:
    exclude_dirs = {path.resolve() for path in (exclude_dirs or set())}
    status_path = DISTRICT_OUTPUT_ROOT / "requested_district_status.csv"
    if not status_path.exists():
        return None

    status_df = pd.read_csv(status_path, low_memory=False)
    frames: list[pd.DataFrame] = []
    for row in status_df.itertuples(index=False):
        output_dir = Path(str(getattr(row, "output_dir", ""))).resolve()
        if output_dir in exclude_dirs:
            continue
        tower_path = output_dir / "data" / "opencellid_district.csv.gz"
        if not tower_path.exists() or tower_path.stat().st_size <= 200:
            continue
        try:
            towers = pd.read_csv(tower_path, low_memory=False)
        except Exception:
            continue
        if towers.empty or not {"lat", "lon"}.issubset(towers.columns):
            continue
        towers = towers.dropna(subset=["lat", "lon"]).copy()
        if towers.empty:
            continue
        for column in ["radio", "mcc", "net", "area", "cell", "range", "samples", "changeable", "averageSignal", "operator"]:
            if column not in towers.columns:
                towers[column] = pd.NA
        frames.append(
            towers[["radio", "mcc", "net", "area", "cell", "lat", "lon", "range", "samples", "changeable", "averageSignal", "operator"]]
        )

    if not frames:
        return None

    SHARED_TOWER_POOL.parent.mkdir(parents=True, exist_ok=True)
    pool = pd.concat(frames, ignore_index=True).drop_duplicates(subset=["radio", "mcc", "net", "area", "cell", "lat", "lon"])
    archive_existing_path(SHARED_TOWER_POOL)
    pool.to_csv(SHARED_TOWER_POOL, index=False, compression="gzip")
    return SHARED_TOWER_POOL


def looks_like_orphan_source_row(row: pd.Series) -> bool:
    block_norm = normalize_text(row.get("block"))
    village_norm = normalize_text(row.get("village"))
    lgd_code = str(row.get("lgd_code") or "").strip()
    source_rows = int(pd.to_numeric(row.get("source_rows"), errors="coerce") or 0)
    gps_rows = int(pd.to_numeric(row.get("gps_rows"), errors="coerce") or 0)
    if block_norm in {"other", "unknown", "district proxy"}:
        return True
    if re.fullmatch(r"v\d+", village_norm or ""):
        return True
    if lgd_code in {"0", "1"} and source_rows <= 1 and gps_rows <= 1 and len(village_norm) <= 6:
        return True
    return False


def annotate_orphan_like_rows(villages_df: pd.DataFrame) -> pd.DataFrame:
    villages = villages_df.copy()
    if villages.empty:
        return villages
    orphan_mask = villages.apply(looks_like_orphan_source_row, axis=1)
    if orphan_mask.any():
        villages.loc[orphan_mask, "is_proxy"] = True
        villages.loc[orphan_mask, "row_kind"] = "source_proxy"
        villages.loc[orphan_mask, "display_village"] = villages.loc[orphan_mask, "district"].astype(str) + " source anchor"
        villages.loc[orphan_mask, "coordinate_source"] = "source_proxy"
    return villages


def prepare_state_assets(base_config: dict, state_villages: pd.DataFrame, state_dir: Path) -> Path:
    state_dir.mkdir(parents=True, exist_ok=True)
    state_ookla_path = state_dir / "ookla_state.geojson"

    villages_with_coords = state_villages.dropna(subset=["latitude", "longitude"]).copy()
    if villages_with_coords.empty:
        raise ValueError(f"No GPS-derived village coordinates available for state {state_villages['state'].iloc[0]}.")

    if not state_ookla_path.exists():
        ensure_ookla_subset(villages_with_coords, state_ookla_path)
    return state_ookla_path


def provider_csv_has_tower_evidence(provider_csv_path: Path) -> bool:
    if not provider_csv_path.exists():
        return False
    df = pd.read_csv(provider_csv_path, usecols=["nearest_tower_km"])
    return bool(df["nearest_tower_km"].notna().any())


def existing_district_dir_candidates(requested_state: str, requested_district: str, matched_state: str, matched_district: str) -> list[Path]:
    candidates = [
        DISTRICT_OUTPUT_ROOT / f"{slugify(requested_state)}__{slugify(requested_district)}",
        DISTRICT_OUTPUT_ROOT / f"{slugify(matched_state)}__{slugify(matched_district)}",
    ]
    deduped: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        if candidate not in seen:
            deduped.append(candidate)
            seen.add(candidate)
    return deduped


def reuse_existing_district_outputs(requested_dir: Path, candidate_dirs: list[Path]) -> bool:
    requested_provider_csv = requested_dir / "outputs" / "village_provider_signal_estimate.csv"
    if provider_csv_has_tower_evidence(requested_provider_csv):
        return True

    for candidate_dir in candidate_dirs:
        candidate_provider_csv = candidate_dir / "outputs" / "village_provider_signal_estimate.csv"
        if not provider_csv_has_tower_evidence(candidate_provider_csv):
            continue
        if candidate_dir.resolve() == requested_dir.resolve():
            return True
        requested_dir.mkdir(parents=True, exist_ok=True)
        for child in candidate_dir.iterdir():
            target = requested_dir / child.name
            if child.is_dir():
                shutil.copytree(child, target, dirs_exist_ok=True)
            else:
                shutil.copy2(child, target)
        return True
    return False


def run_district_pipeline(
    base_config: dict,
    district_villages: pd.DataFrame,
    district_dir: Path,
    shared_ookla_path: Path,
    opencellid_tower_path: Path | None = None,
) -> dict[str, str]:
    district_dir.mkdir(parents=True, exist_ok=True)
    data_dir = district_dir / "data"
    output_dir = district_dir / "outputs"
    data_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    villages_path = data_dir / "villages.csv"
    tower_path = data_dir / "opencellid_district.csv.gz"
    if opencellid_tower_path is not None:
        if opencellid_tower_path.resolve() != tower_path.resolve():
            archive_existing_path(tower_path)
            shutil.copy2(opencellid_tower_path, tower_path)
    else:
        tower_path = data_dir / "opencellid_district.csv.gz"

    district_villages.to_csv(villages_path, index=False)

    config = json.loads(json.dumps(base_config))
    config["filters"] = {
        "state": str(district_villages["state"].iloc[0]),
        "district": str(district_villages["district"].iloc[0]),
        "block": "",
    }
    config["paths"]["village_input"] = str(villages_path)
    config["paths"]["village_master"] = str(data_dir / "missing_village_master.csv")
    config["paths"]["opencellid_towers"] = str(tower_path)
    config["paths"]["ookla_tiles"] = str(shared_ookla_path)
    config["paths"]["output_dir"] = str(output_dir)
    if opencellid_tower_path is not None:
        config["distance"]["search_radius_km"] = float(config["distance"].get("shared_pool_search_radius_km", config["distance"].get("search_radius_km", 10)))

    try:
        outputs = run_pipeline(config, ROOT)
    except ValueError as exc:
        if "OpenCellID area API returned no towers" not in str(exc):
            raise
        empty_towers = pd.DataFrame(
            columns=["radio", "mcc", "net", "area", "cell", "lat", "lon", "operator", "averageSignal"]
        )
        empty_towers.to_csv(tower_path, index=False, compression="gzip")
        outputs = run_pipeline(config, ROOT)
    map_path = output_dir / "village_connectivity_map.html"
    subprocess.run(
        [
            str(ROOT / ".venv" / "Scripts" / "python.exe"),
            str(ROOT / "generate_provider_map.py"),
            "--input-csv",
            str(outputs.provider_csv),
            "--output-html",
            str(map_path),
        ],
        check=True,
        cwd=str(ROOT),
    )
    return {
        "provider_csv": str(outputs.provider_csv),
        "summary_xlsx": str(outputs.summary_xlsx),
        "geojson": str(outputs.villages_geojson),
        "map_html": str(map_path),
    }


def score_rank(score: str) -> int:
    order = {"Strong": 4, "Moderate": 3, "Weak": 2, "Unknown": 1}
    return order.get(str(score), 0)


def enrich_rows(rows: pd.DataFrame, provider_csv_path: Path) -> pd.DataFrame:
    provider_df = pd.read_csv(provider_csv_path)
    provider_df["state"] = provider_df["state"].map(canonicalize_state)
    provider_df["district"] = provider_df["district"].map(canonicalize_district)
    provider_df["block"] = provider_df["block"].fillna("").astype(str).str.strip()
    provider_df["village"] = provider_df["village"].fillna("").astype(str).str.strip()

    pivot = provider_df.pivot_table(
        index=["state", "district", "block", "village"],
        columns="provider",
        values="coverage_score",
        aggfunc="first",
    ).reset_index()
    pivot.columns = [
        col if isinstance(col, str) else col
        for col in pivot.columns
    ]
    rename_map = {
        "Airtel": "network_airtel_score",
        "BSNL": "network_bsnl_score",
        "Jio": "network_jio_score",
        "Vodafone Idea": "network_vodafone_idea_score",
    }
    pivot = pivot.rename(columns=rename_map)

    enriched = rows.copy()
    enriched["state"] = enriched["login_info-state_name"].map(canonicalize_state)
    enriched["district"] = enriched["login_info-district_name"].map(canonicalize_district)
    enriched["block"] = enriched["beneficiary_info-block"].fillna("").astype(str).str.strip()
    enriched["village"] = enriched["beneficiary_info-village"].fillna("").astype(str).str.strip()
    enriched = enriched.merge(pivot, on=["state", "district", "block", "village"], how="left")

    score_cols = [
        "network_airtel_score",
        "network_bsnl_score",
        "network_jio_score",
        "network_vodafone_idea_score",
    ]
    available_cols = [col for col in score_cols if col in enriched.columns]
    enriched["network_best_score"] = enriched[available_cols].apply(
        lambda row: max((value for value in row if isinstance(value, str)), key=score_rank, default="Unknown"),
        axis=1,
    )
    enriched["network_canonical_state"] = enriched["state"]
    enriched["network_canonical_district"] = enriched["district"]
    return enriched


def build_site(summary_rows: list[dict[str, object]], missing: list[tuple[str, str]]) -> None:
    SITE_ROOT.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            str(ROOT / ".venv" / "Scripts" / "python.exe"),
            str(ROOT / "report_requested_district_status.py"),
        ],
        check=True,
        cwd=str(ROOT),
    )
    subprocess.run(
        [
            str(ROOT / ".venv" / "Scripts" / "python.exe"),
            str(ROOT / "generate_requested_districts_site.py"),
        ],
        check=True,
        cwd=str(ROOT),
    )


def write_csv_with_fallback(df: pd.DataFrame, path: Path) -> Path:
    try:
        archive_existing_path(path)
        df.to_csv(path, index=False)
        return path
    except PermissionError:
        fallback = path.with_name(f"{path.stem}__latest{path.suffix}")
        df.to_csv(fallback, index=False)
        return fallback


def merge_on_requested_district(existing: pd.DataFrame | None, new: pd.DataFrame, key_columns: list[str]) -> pd.DataFrame:
    if existing is None or existing.empty:
        return new.copy()
    combined = pd.concat([existing, new], ignore_index=True)
    available_keys = [column for column in key_columns if column in combined.columns]
    if not available_keys:
        return combined.drop_duplicates(keep="last")
    combined = combined.drop_duplicates(subset=available_keys, keep="last")
    return combined


def main() -> None:
    parser = argparse.ArgumentParser(description="Enrich requested districts from StopTB screening rows using OpenCellID and Ookla.")
    parser.add_argument("--requested-state", default="", help="Limit processing to a single requested state label.")
    parser.add_argument("--requested-district", default="", help="Limit processing to a single requested district label.")
    args = parser.parse_args()

    DISTRICT_OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    base_config = load_config(BASE_CONFIG)
    requested_districts = load_requested_districts()
    requested_facility_df = load_requested_facility_rows()
    if args.requested_state or args.requested_district:
        requested_districts = [
            item for item in requested_districts
            if requested_matches_filter(item, args.requested_state, args.requested_district)
        ]
        if not requested_districts:
            raise ValueError("No requested districts matched the provided requested-state/requested-district filter.")
    source = pd.read_csv(SOURCE_CSV, low_memory=False)
    source["canonical_state"] = source["login_info-state_name"].map(canonicalize_state)
    source["canonical_district"] = source["login_info-district_name"].map(canonicalize_district)

    requested_lookup = {
        district_key(item["matched_state"], item["matched_district"]): item
        for item in requested_districts
    }
    source["requested_match"] = source.apply(
        lambda row: district_key(row["canonical_state"], row["canonical_district"]) in requested_lookup,
        axis=1,
    )
    requested_rows = source[source["requested_match"]].copy()

    present_pairs = {
        district_key(state, district)
        for state, district in requested_rows[["canonical_state", "canonical_district"]].drop_duplicates().itertuples(index=False)
    }
    missing = [
        {"state": item["requested_state"], "district": item["requested_district"]}
        for item in requested_districts
        if district_key(item["matched_state"], item["matched_district"]) not in present_pairs
    ]

    village_master = build_village_master(requested_rows) if not requested_rows.empty else pd.DataFrame(
        columns=["state", "district", "block", "village", "lgd_code", "latitude", "longitude", "source_rows", "gps_rows"]
    )
    status_lookup = {}
    if STATUS_CSV.exists():
        status_df = pd.read_csv(STATUS_CSV, low_memory=False)
        status_lookup = {
            (str(row.requested_state), str(row.requested_district)): str(row.status)
            for row in status_df.itertuples(index=False)
        }
    shared_tower_pool = build_shared_tower_pool()
    state_assets: dict[str, Path] = {}

    summary_rows: list[dict[str, object]] = []
    enriched_frames: list[pd.DataFrame] = []

    for item in requested_districts:
        requested_state = item["requested_state"]
        requested_district = item["requested_district"]
        matched_state = item["matched_state"]
        matched_district = item["matched_district"]
        key = district_key(matched_state, matched_district)
        has_source_rows = key in present_pairs
        district_rows = requested_rows[
            (requested_rows["canonical_state"].map(normalize_text) == key[0])
            & (requested_rows["canonical_district"].map(normalize_text) == key[1])
        ].copy() if has_source_rows else pd.DataFrame()
        district_villages = village_master[
            (village_master["state"].map(normalize_text) == key[0])
            & (village_master["district"].map(normalize_text) == key[1])
        ].copy() if has_source_rows else build_lgd_fallback_villages(item, requested_facility_df)
        if district_villages.empty:
            continue
        if has_source_rows:
            district_villages = annotate_orphan_like_rows(district_villages)
        state_ookla_path = state_assets.get(matched_state)
        if state_ookla_path is None and district_villages["latitude"].notna().any() and district_villages["longitude"].notna().any():
            state_ookla_path = prepare_state_assets(base_config, district_villages, STATE_CACHE_ROOT / slugify(matched_state))
            state_assets[matched_state] = state_ookla_path
        if state_ookla_path is None:
            continue
        district_slug = f"{slugify(requested_state)}__{slugify(requested_district)}"
        district_dir = DISTRICT_OUTPUT_ROOT / district_slug
        candidate_dirs = existing_district_dir_candidates(requested_state, requested_district, matched_state, matched_district)
        reused_existing = reuse_existing_district_outputs(district_dir, candidate_dirs)
        used_shared_tower_pool = False
        if args.requested_state or args.requested_district:
            reused_existing = False
        if not reused_existing and district_dir.exists():
            archive_existing_tree(district_dir)
            shutil.rmtree(district_dir)
        if reused_existing:
            output_paths = {
                "provider_csv": str(district_dir / "outputs" / "village_provider_signal_estimate.csv"),
                "summary_xlsx": str(district_dir / "outputs" / "village_connectivity_summary.xlsx"),
                "geojson": str(district_dir / "outputs" / "village_connectivity.geojson"),
                "map_html": str(district_dir / "outputs" / "village_connectivity_map.html"),
            }
        else:
            current_status = status_lookup.get((requested_state, requested_district), "")
            should_prefer_shared_pool = (
                has_source_rows
                and current_status in {"pending_fetch", "fetched_no_tower_evidence", "fallback_no_tower_evidence"}
                and shared_tower_pool is not None
            )
            try:
                if should_prefer_shared_pool:
                    output_paths = run_district_pipeline(
                        base_config,
                        district_villages,
                        district_dir,
                        state_ookla_path,
                        opencellid_tower_path=shared_tower_pool,
                    )
                    used_shared_tower_pool = True
                else:
                    output_paths = run_district_pipeline(base_config, district_villages, district_dir, state_ookla_path)
                    provider_csv_path = Path(output_paths["provider_csv"])
                    if has_source_rows and not provider_csv_has_tower_evidence(provider_csv_path):
                        shared_pool_for_retry = shared_tower_pool or build_shared_tower_pool(exclude_dirs={district_dir})
                        if shared_pool_for_retry is not None:
                            archive_existing_tree(district_dir)
                            shutil.rmtree(district_dir)
                            output_paths = run_district_pipeline(
                                base_config,
                                district_villages,
                                district_dir,
                                state_ookla_path,
                                opencellid_tower_path=shared_pool_for_retry,
                            )
                            used_shared_tower_pool = True
            except ValueError as exc:
                shared_retry_needed = (
                    has_source_rows
                    and shared_tower_pool is not None
                    and "OpenCellID area API returned no towers" in str(exc)
                )
                if not shared_retry_needed:
                    raise
                if district_dir.exists():
                    archive_existing_tree(district_dir)
                    shutil.rmtree(district_dir)
                output_paths = run_district_pipeline(
                    base_config,
                    district_villages,
                    district_dir,
                    state_ookla_path,
                    opencellid_tower_path=shared_tower_pool,
                )
                used_shared_tower_pool = True
        if has_source_rows:
            enriched = enrich_rows(district_rows, Path(output_paths["provider_csv"]))
            enriched["network_map_path"] = str(Path(output_paths["map_html"]).resolve())
            enriched["requested_state"] = requested_state
            enriched["requested_district"] = requested_district
            enriched["matched_state"] = matched_state
            enriched["matched_district"] = matched_district
            enriched["network_source_mode"] = (
                "reused_existing_tower_enrichment"
                if reused_existing
                else "shared_tower_pool_enrichment" if used_shared_tower_pool else "fresh_opencellid_fetch"
            )
            enriched_frames.append(enriched)
        source_mode = "reused_existing_tower_enrichment" if reused_existing else "fresh_opencellid_fetch"
        if not has_source_rows:
            source_mode = "district_fallback_proxy_geocoded"
        elif used_shared_tower_pool:
            source_mode = "shared_tower_pool_enriched"
        summary_rows.append(
            {
                "state": requested_state,
                "district": requested_district,
                "matched_state": matched_state,
                "matched_district": matched_district,
                "source_mode": source_mode,
                "row_count": int(len(district_rows)),
                "village_count": int(len(district_villages)),
                "gps_village_count": int(district_villages["latitude"].notna().sum()) if has_source_rows else 0,
                "relative_map_html": os.path.relpath(output_paths["map_html"], SITE_ROOT).replace("\\", "/"),
                "relative_provider_csv": os.path.relpath(output_paths["provider_csv"], SITE_ROOT).replace("\\", "/"),
            }
        )

    if enriched_frames:
        combined = pd.concat(enriched_frames, ignore_index=True)
        combined_output_path = DISTRICT_OUTPUT_ROOT / "requested_district_rows_enriched.csv"
        existing_combined = pd.read_csv(combined_output_path, low_memory=False) if combined_output_path.exists() else None
        combined_merged = merge_on_requested_district(
            existing_combined,
            combined,
            ["requested_state", "requested_district", "beneficiary_id"] if "beneficiary_id" in combined.columns else ["requested_state", "requested_district", "state", "district", "block", "village", "provider"],
        )
        combined_path = write_csv_with_fallback(combined_merged, combined_output_path)
        print(f"Wrote enriched rows to {combined_path}")

    if summary_rows:
        summary = pd.DataFrame(summary_rows)
        summary_output_path = DISTRICT_OUTPUT_ROOT / "requested_district_summary.csv"
        existing_summary = pd.read_csv(summary_output_path) if summary_output_path.exists() else None
        summary_merged = merge_on_requested_district(existing_summary, summary, ["state", "district"])
        summary_path = write_csv_with_fallback(summary_merged, summary_output_path)
        summary_json = {
            "requested_district_count": len(load_requested_districts()),
            "present_district_count": len(summary_merged),
            "processing_mode": "gps_plus_opencellid_ookla",
            "missing_districts": missing,
        }
        archive_existing_path(DISTRICT_OUTPUT_ROOT / "requested_district_summary.json")
        (DISTRICT_OUTPUT_ROOT / "requested_district_summary.json").write_text(json.dumps(summary_json, indent=2), encoding="utf-8")
        build_site(summary_merged.to_dict("records"), missing)
        print(f"Wrote summary to {summary_path}")
        print(f"Wrote site index to {SITE_ROOT / 'index.html'}")
    else:
        print("No requested districts were present in the screening source file.")


if __name__ == "__main__":
    main()
