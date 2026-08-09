from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import shutil
import time
from datetime import UTC, datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import geopandas as gpd
import pandas as pd
import requests
import yaml
from openpyxl import load_workbook
from pyproj import CRS
from shapely.geometry import Point, box

from archive_utils import archive_existing_path
from source_metadata import file_mtime_iso, recency_payload, series_latest_iso
from .providers import normalize_provider_name, provider_from_codes
from .google_sheets_sync import sync_pipeline_outputs


REQUIRED_VILLAGE_COLUMNS = ["state", "district", "block", "village", "lgd_code"]


@dataclass
class PipelineOutputs:
    provider_csv: Path
    summary_xlsx: Path
    villages_geojson: Path
    google_sheets_synced: bool = False
    google_sheets_tabs: list[str] | None = None
    run_id: str | None = None


def main() -> None:
    parser = argparse.ArgumentParser(description="Estimate mobile connectivity by provider for Koraput block villages.")
    parser.add_argument(
        "--config",
        default=Path(__file__).resolve().parent.parent / "config.yaml",
        type=Path,
        help="Path to the YAML configuration file.",
    )
    args = parser.parse_args()

    config = load_config(args.config)
    outputs = run_pipeline(config, args.config.parent)
    print(json.dumps({k: str(v) for k, v in outputs.__dict__.items()}, indent=2))


def load_config(config_path: Path) -> dict[str, Any]:
    with config_path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def run_pipeline(config: dict[str, Any], base_dir: Path) -> PipelineOutputs:
    output_dir = resolve_path(base_dir, config["paths"]["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)

    villages_df = load_table(resolve_path(base_dir, config["paths"]["village_input"]))
    villages_df = standardize_columns(villages_df, config["columns"]["villages"])
    validate_columns(villages_df, REQUIRED_VILLAGE_COLUMNS, "village input")
    villages_df = filter_villages(villages_df, config.get("filters", {}))

    village_master_df = load_optional_table(resolve_path(base_dir, config["paths"]["village_master"]))
    if village_master_df is not None:
        village_master_df = standardize_columns(village_master_df, config["columns"]["village_master"])
    else:
        village_master_df = pd.DataFrame(columns=villages_df.columns)
    block_centroid_path = Path(__file__).resolve().parent.parent / "data" / "cache" / "lgd" / "nominatim_block_centroids.csv"
    villages_df = fill_missing_coordinates(villages_df, village_master_df, block_centroid_path)

    if villages_df["latitude"].isna().any() or villages_df["longitude"].isna().any():
        missing_count = int((villages_df["latitude"].isna() | villages_df["longitude"].isna()).sum())
        raise ValueError(f"Village coordinates are still missing after LGD/master lookup for {missing_count} rows.")

    villages_df = villages_df.reset_index(drop=True)
    villages_df["village_id"] = villages_df.index + 1
    villages_gdf = to_points_gdf(villages_df, "longitude", "latitude")

    tower_path = resolve_path(base_dir, config["paths"]["opencellid_towers"])
    ensure_tower_data(config, tower_path, villages_gdf)
    towers_df = load_table(tower_path)
    towers_df = standardize_columns(towers_df, config["columns"]["towers"])
    source_recency = build_source_recency(towers_df, tower_path, resolve_path(base_dir, config["paths"]["ookla_tiles"]))
    towers_df = prepare_towers(
        towers_df,
        config.get("provider_mapping", {}),
        config.get("legacy_provider_policy", {}),
        config.get("current_operators", []),
    )
    towers_gdf = to_points_gdf(towers_df, "longitude", "latitude")

    nearby_towers = find_towers_within_radius(
        villages_gdf=villages_gdf,
        towers_gdf=towers_gdf,
        radius_km=float(config["distance"]["search_radius_km"]),
    )

    provider_distances = summarize_provider_distances(nearby_towers)
    village_level_ookla, provider_level_ookla = join_ookla(
        villages_gdf,
        resolve_path(base_dir, config["paths"]["ookla_tiles"]),
        config["columns"]["ookla"],
        float(config.get("ookla", {}).get("nearest_tile_max_km", 5)),
    )
    final_provider_rows = build_provider_rows(
        villages_df=villages_df,
        provider_distances=provider_distances,
        village_level_ookla=village_level_ookla,
        provider_level_ookla=provider_level_ookla,
        config=config,
        source_recency=source_recency,
    )

    provider_csv = output_dir / "village_provider_signal_estimate.csv"
    summary_xlsx = output_dir / "village_connectivity_summary.xlsx"
    villages_geojson = output_dir / "village_connectivity.geojson"

    archive_existing_path(provider_csv)
    final_provider_rows.to_csv(provider_csv, index=False)
    archive_existing_path(summary_xlsx)
    summary_xlsx = write_summary_workbook(final_provider_rows, summary_xlsx)
    archive_existing_path(villages_geojson)
    write_geojson(villages_gdf, final_provider_rows, villages_geojson)

    google_sheets_result = sync_pipeline_outputs(
        config=config,
        provider_rows=final_provider_rows,
        source_recency=source_recency,
    )

    return PipelineOutputs(
        provider_csv=provider_csv,
        summary_xlsx=summary_xlsx,
        villages_geojson=villages_geojson,
        google_sheets_synced=bool(google_sheets_result.get("synced")),
        google_sheets_tabs=list(google_sheets_result.get("tabs", [])),
        run_id=str(google_sheets_result.get("run_id") or ""),
    )


def resolve_path(base_dir: Path, raw_path: str) -> Path:
    path = Path(raw_path)
    if path.is_absolute():
        return path
    return (base_dir / path).resolve()


def load_table(path: Path) -> pd.DataFrame:
    suffix = path.suffix.lower()
    if suffix in {".xlsx", ".xlsm", ".xls"}:
        return pd.read_excel(path)
    if suffix == ".csv":
        return pd.read_csv(path)
    if suffix == ".gz" and path.name.lower().endswith(".csv.gz"):
        return pd.read_csv(path, compression="gzip")
    if suffix == ".parquet":
        return pd.read_parquet(path)
    raise ValueError(f"Unsupported input file format: {path}")


def load_optional_table(path: Path) -> pd.DataFrame | None:
    return load_table(path) if path.exists() else None


def build_source_recency(towers_df: pd.DataFrame, tower_path: Path, ookla_path: Path) -> dict[str, str]:
    opencellid_value = ""
    if "updated" in towers_df.columns:
        opencellid_value = series_latest_iso(towers_df["updated"])
    if not opencellid_value and "created" in towers_df.columns:
        opencellid_value = series_latest_iso(towers_df["created"])
    if not opencellid_value:
        opencellid_value = file_mtime_iso(tower_path)

    ookla_value = file_mtime_iso(ookla_path)
    generated_at = datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")
    return recency_payload(opencellid_value, ookla_value, generated_at=generated_at)


def standardize_columns(df: pd.DataFrame, column_mapping: dict[str, str]) -> pd.DataFrame:
    inverted = {source: target for target, source in column_mapping.items()}
    renamed = df.rename(columns=inverted).copy()
    renamed.columns = [str(col).strip() for col in renamed.columns]
    return renamed


def validate_columns(df: pd.DataFrame, required_columns: list[str], label: str) -> None:
    missing = [column for column in required_columns if column not in df.columns]
    if missing:
        raise ValueError(f"Missing required columns in {label}: {missing}")


def normalize_text_series(series: pd.Series) -> pd.Series:
    return (
        series.fillna("")
        .astype(str)
        .str.strip()
        .str.lower()
        .str.replace(r"[^a-z0-9]+", " ", regex=True)
        .str.replace(r"\s+", " ", regex=True)
        .str.strip()
    )


def filter_villages(df: pd.DataFrame, filters: dict[str, Any]) -> pd.DataFrame:
    filtered = df.copy()
    for column in ["state", "district", "block"]:
        value = filters.get(column)
        if value:
            filtered = filtered[normalize_text_series(filtered[column]) == normalize_text_series(pd.Series([value])).iloc[0]]
    if filtered.empty:
        raise ValueError("No villages matched the configured state/district/block filters.")
    return filtered


def fill_missing_coordinates(
    villages_df: pd.DataFrame,
    village_master_df: pd.DataFrame,
    block_centroid_path: Path | None = None,
) -> pd.DataFrame:
    villages = villages_df.copy()
    master = village_master_df.copy()

    for frame in [villages, master]:
        if "latitude" not in frame.columns:
            frame["latitude"] = pd.NA
        if "longitude" not in frame.columns:
            frame["longitude"] = pd.NA
        frame["latitude"] = pd.to_numeric(frame["latitude"], errors="coerce")
        frame["longitude"] = pd.to_numeric(frame["longitude"], errors="coerce")
        frame["village_norm"] = normalize_text_series(frame["village"])
        frame["block_norm"] = normalize_text_series(frame["block"])
        frame["district_norm"] = normalize_text_series(frame["district"])
        frame["state_norm"] = normalize_text_series(frame["state"])
        frame["lgd_code"] = frame["lgd_code"].astype(str).str.strip()

    lgd_lookup = (
        master.dropna(subset=["latitude", "longitude"])
        .drop_duplicates(subset=["lgd_code"])
        .set_index("lgd_code")[["latitude", "longitude"]]
    )

    villages["coordinate_source"] = "input"
    missing_mask = villages["latitude"].isna() | villages["longitude"].isna()
    if missing_mask.any():
        lgd_matches = villages.loc[missing_mask, "lgd_code"].map(lgd_lookup.to_dict("index"))
        lgd_filled = lgd_matches.apply(
            lambda item: (
                item.get("latitude") if isinstance(item, dict) else pd.NA,
                item.get("longitude") if isinstance(item, dict) else pd.NA,
            )
        )
        lgd_fill_df = pd.DataFrame(lgd_filled.tolist(), index=villages.index[missing_mask], columns=["latitude", "longitude"])
        lgd_fill_df["latitude"] = pd.to_numeric(lgd_fill_df["latitude"], errors="coerce")
        lgd_fill_df["longitude"] = pd.to_numeric(lgd_fill_df["longitude"], errors="coerce")
        villages.loc[missing_mask, ["latitude", "longitude"]] = lgd_fill_df
        resolved_from_lgd = missing_mask & villages["latitude"].notna() & villages["longitude"].notna()
        villages.loc[resolved_from_lgd, "coordinate_source"] = "lgd_master"

    still_missing_mask = villages["latitude"].isna() | villages["longitude"].isna()
    if still_missing_mask.any():
        keyed_master = master.dropna(subset=["latitude", "longitude"]).drop_duplicates(
            subset=["state_norm", "district_norm", "block_norm", "village_norm"]
        )
        merged = villages.loc[still_missing_mask].merge(
            keyed_master[
                [
                    "state_norm",
                    "district_norm",
                    "block_norm",
                    "village_norm",
                    "latitude",
                    "longitude",
                ]
            ],
            on=["state_norm", "district_norm", "block_norm", "village_norm"],
            how="left",
            suffixes=("", "_master"),
        )
        villages.loc[still_missing_mask, "latitude"] = merged["latitude_master"].values
        villages.loc[still_missing_mask, "longitude"] = merged["longitude_master"].values
        resolved_from_name = still_missing_mask & villages["latitude"].notna() & villages["longitude"].notna()
        villages.loc[resolved_from_name, "coordinate_source"] = "lgd_master"

    still_missing_mask = villages["latitude"].isna() | villages["longitude"].isna()
    if still_missing_mask.any() and block_centroid_path is not None and block_centroid_path.exists():
        block_centroids = pd.read_csv(block_centroid_path, low_memory=False)
        if {"query", "latitude", "longitude"}.issubset(block_centroids.columns):
            block_centroids["latitude"] = pd.to_numeric(block_centroids["latitude"], errors="coerce")
            block_centroids["longitude"] = pd.to_numeric(block_centroids["longitude"], errors="coerce")
            block_centroids["query_norm"] = normalize_text_series(block_centroids["query"])
            block_centroid_lookup = (
                block_centroids.dropna(subset=["latitude", "longitude"])
                .drop_duplicates(subset=["query_norm"])
                .set_index("query_norm")[["latitude", "longitude"]]
            )
            block_centroid_lookup_dict = block_centroid_lookup.to_dict("index")
            block_rows = villages.loc[still_missing_mask, ["state", "district", "block"]].astype(str)

            def _lookup_centroid(state: str, district: str, block: str) -> dict[str, float] | None:
                candidates = [
                    f"{block}, {district}, {state}, India",
                    f"{district}, {state}, India",
                    f"{state}, India",
                ]
                for candidate in candidates:
                    normalized = normalize_text_series(pd.Series([candidate])).iloc[0]
                    match = block_centroid_lookup_dict.get(normalized)
                    if match is not None:
                        return match
                return None

            block_matches = block_rows.apply(lambda row: _lookup_centroid(row["state"], row["district"], row["block"]), axis=1)
            block_filled = block_matches.apply(
                lambda item: (
                    item.get("latitude") if isinstance(item, dict) else pd.NA,
                    item.get("longitude") if isinstance(item, dict) else pd.NA,
                )
            )
            block_fill_df = pd.DataFrame(block_filled.tolist(), index=villages.index[still_missing_mask], columns=["latitude", "longitude"])
            block_fill_df["latitude"] = pd.to_numeric(block_fill_df["latitude"], errors="coerce")
            block_fill_df["longitude"] = pd.to_numeric(block_fill_df["longitude"], errors="coerce")
            villages.loc[still_missing_mask, ["latitude", "longitude"]] = block_fill_df
            resolved_from_block = still_missing_mask & villages["latitude"].notna() & villages["longitude"].notna()
            villages.loc[resolved_from_block, "coordinate_source"] = "block_centroid"

    unresolved = villages["latitude"].isna() | villages["longitude"].isna()
    if unresolved.any():
        villages.loc[unresolved, "coordinate_source"] = "missing"

    return villages.drop(columns=["village_norm", "block_norm", "district_norm", "state_norm"])


def to_points_gdf(df: pd.DataFrame, lon_col: str, lat_col: str) -> gpd.GeoDataFrame:
    clean = df.copy()
    clean[lon_col] = pd.to_numeric(clean[lon_col], errors="coerce")
    clean[lat_col] = pd.to_numeric(clean[lat_col], errors="coerce")
    clean = clean.dropna(subset=[lon_col, lat_col]).copy()
    clean["geometry"] = [Point(xy) for xy in zip(clean[lon_col], clean[lat_col], strict=False)]
    return gpd.GeoDataFrame(clean, geometry="geometry", crs="EPSG:4326")


def ensure_tower_data(config: dict[str, Any], tower_path: Path, villages_gdf: gpd.GeoDataFrame) -> None:
    if tower_path.exists():
        if tower_file_has_rows(tower_path):
            seed_opencellid_extent_cache(config, tower_path, villages_gdf)
            return
        archive_existing_path(tower_path)
        tower_path.unlink(missing_ok=True)
    if tower_path.exists():
        seed_opencellid_extent_cache(config, tower_path, villages_gdf)
        return

    api_config = config.get("api", {}).get("opencellid", {})
    if not api_config.get("enabled"):
        raise FileNotFoundError(
            f"OpenCellID tower file not found at {tower_path}. Add the local file or enable the download configuration."
        )

    if api_config.get("mode") == "area_api":
        download_opencellid_area_data(config, villages_gdf, tower_path)
        return

    download_url = api_config.get("download_url")
    if not download_url:
        raise ValueError("OpenCellID download is enabled but no download_url was provided in config.yaml.")

    headers = dict(api_config.get("request_headers", {}))
    if api_config.get("token"):
        headers.setdefault("Authorization", f"Bearer {api_config['token']}")

    response = requests.get(download_url, headers=headers, timeout=120)
    response.raise_for_status()
    tower_path.parent.mkdir(parents=True, exist_ok=True)
    tower_path.write_bytes(response.content)


def download_opencellid_area_data(config: dict[str, Any], villages_gdf: gpd.GeoDataFrame, tower_path: Path) -> None:
    api_config = config.get("api", {}).get("opencellid", {})
    token = str(api_config.get("token") or "").strip()
    if not token:
        raise ValueError("OpenCellID area API mode requires api.opencellid.token in config.yaml.")

    search_radius_km = float(config["distance"]["search_radius_km"])
    expanded_search_radius_km = float(config["distance"].get("expanded_search_radius_km", search_radius_km))
    tile_size_deg = float(api_config.get("area_tile_size_deg", 0.018))
    max_bbox_area_sq_m = float(api_config.get("max_bbox_area_sq_m", 4_000_000))
    page_limit = int(api_config.get("page_limit", 50))
    pause_seconds = float(api_config.get("pause_seconds", 0.05))
    workers = max(int(api_config.get("workers", 6)), 1)

    towers = fetch_opencellid_area_tiles(
        config=config,
        villages_gdf=villages_gdf,
        search_radius_km=search_radius_km,
        tile_size_deg=tile_size_deg,
        max_bbox_area_sq_m=max_bbox_area_sq_m,
        page_limit=page_limit,
        pause_seconds=pause_seconds,
        workers=workers,
        tower_path=tower_path,
    )
    used_search_radius_km = search_radius_km
    if towers.empty and expanded_search_radius_km > search_radius_km:
        print(
            f"OpenCellID returned no towers for the {search_radius_km:.1f} km buffered extent; "
            f"retrying at {expanded_search_radius_km:.1f} km."
        )
        towers = fetch_opencellid_area_tiles(
            config=config,
            villages_gdf=villages_gdf,
            search_radius_km=expanded_search_radius_km,
            tile_size_deg=tile_size_deg,
            max_bbox_area_sq_m=max_bbox_area_sq_m,
            page_limit=page_limit,
            pause_seconds=pause_seconds,
            workers=workers,
            tower_path=tower_path,
        )
        used_search_radius_km = expanded_search_radius_km

    towers = towers.rename(
        columns={
            "mnc": "net",
            "lac": "area",
            "cellid": "cell",
            "averageSignalStrength": "averageSignal",
        }
    )
    keep_columns = ["radio", "mcc", "net", "area", "cell", "lon", "lat", "range", "samples", "changeable", "averageSignal"]
    for column in keep_columns:
        if column not in towers.columns:
            towers[column] = pd.NA
    towers = towers[keep_columns].drop_duplicates(subset=["radio", "mcc", "net", "area", "cell", "lon", "lat"])
    tower_path.parent.mkdir(parents=True, exist_ok=True)
    towers.to_csv(tower_path, index=False, compression="gzip")
    buffered_area = compute_buffered_area(villages_gdf, used_search_radius_km)
    cache_path = resolve_opencellid_extent_cache_path(config, tower_path, buffered_area.bounds, used_search_radius_km)
    if cache_path is not None:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(tower_path, cache_path)


def fetch_opencellid_area_tiles(
    config: dict[str, Any],
    villages_gdf: gpd.GeoDataFrame,
    search_radius_km: float,
    tile_size_deg: float,
    max_bbox_area_sq_m: float,
    page_limit: int,
    pause_seconds: float,
    workers: int,
    tower_path: Path,
) -> pd.DataFrame:
    api_config = config.get("api", {}).get("opencellid", {})
    token = str(api_config.get("token") or "").strip()
    buffered_area = compute_buffered_area(villages_gdf, search_radius_km)
    cache_path = resolve_opencellid_extent_cache_path(config, tower_path, buffered_area.bounds, search_radius_km)
    if cache_path is not None and cache_path.exists():
        tower_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(cache_path, tower_path)
        print(f"Reused cached OpenCellID extent extract: {cache_path}")
        return pd.read_csv(tower_path, low_memory=False)

    tiles = build_bbox_tiles(buffered_area, tile_size_deg, max_bbox_area_sq_m)
    all_rows: list[pd.DataFrame] = []
    print(f"Fetching OpenCellID area tiles: {len(tiles)}")

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [
            executor.submit(fetch_opencellid_tile_rows, token, bbox, page_limit, pause_seconds)
            for bbox in tiles
        ]
        for index, future in enumerate(as_completed(futures), start=1):
            tile_rows = future.result()
            if not tile_rows.empty:
                all_rows.append(tile_rows)
            if index % 25 == 0 or index == len(futures):
                print(f"Completed {index}/{len(futures)} OpenCellID tiles")

    if not all_rows:
        return pd.DataFrame()

    towers = pd.concat(all_rows, ignore_index=True)
    return towers


def tower_file_has_rows(path: Path) -> bool:
    try:
        df = pd.read_csv(path, usecols=["lat", "lon"], low_memory=False)
    except Exception:
        return False
    if df.empty:
        return False
    return bool(pd.to_numeric(df["lat"], errors="coerce").notna().any() and pd.to_numeric(df["lon"], errors="coerce").notna().any())


def resolve_opencellid_extent_cache_path(
    config: dict[str, Any],
    tower_path: Path,
    bounds: tuple[float, float, float, float],
    search_radius_km: float,
) -> Path | None:
    cache_config = config.get("cache", {})
    if not cache_config.get("enabled", True) or not cache_config.get("reuse_opencellid_extent_cache", True):
        return None

    raw_cache_dir = config.get("paths", {}).get("cache_dir")
    if not raw_cache_dir:
        return None

    cache_dir = resolve_path(tower_path.parent.parent, raw_cache_dir) / "opencellid"
    extent_key = "|".join([*(f"{value:.6f}" for value in bounds), f"{search_radius_km:.2f}"])
    digest = hashlib.sha1(extent_key.encode("utf-8")).hexdigest()[:16]
    return cache_dir / f"opencellid_extent_{digest}.csv.gz"


def seed_opencellid_extent_cache(config: dict[str, Any], tower_path: Path, villages_gdf: gpd.GeoDataFrame) -> None:
    search_radius_km = float(config["distance"]["search_radius_km"])
    buffered_area = compute_buffered_area(villages_gdf, search_radius_km)
    cache_path = resolve_opencellid_extent_cache_path(config, tower_path, buffered_area.bounds, search_radius_km)
    if cache_path is None or cache_path.exists():
        return
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(tower_path, cache_path)


def compute_buffered_area(villages_gdf: gpd.GeoDataFrame, buffer_km: float):
    projected_crs = estimate_projected_crs(villages_gdf)
    buffered = villages_gdf.to_crs(projected_crs).geometry.buffer(buffer_km * 1000).union_all()
    return gpd.GeoSeries([buffered], crs=projected_crs).to_crs("EPSG:4326").iloc[0]


def build_bbox_tiles(buffered_area, tile_size_deg: float, max_bbox_area_sq_m: float) -> list[tuple[float, float, float, float]]:
    min_lon, min_lat, max_lon, max_lat = buffered_area.bounds
    center_lat = (min_lat + max_lat) / 2
    lat_m = 111_320
    lon_m = max(111_320 * math.cos(math.radians(center_lat)), 1)
    if (tile_size_deg * lat_m) * (tile_size_deg * lon_m) > max_bbox_area_sq_m:
        raise ValueError("Configured OpenCellID tile size exceeds the API bbox area limit.")

    tiles: list[tuple[float, float, float, float]] = []
    lat = min_lat
    while lat < max_lat:
        next_lat = min(lat + tile_size_deg, max_lat)
        lon = min_lon
        while lon < max_lon:
            next_lon = min(lon + tile_size_deg, max_lon)
            tile_geom = box(lon, lat, next_lon, next_lat)
            if tile_geom.intersects(buffered_area):
                tiles.append((lat, lon, next_lat, next_lon))
            lon = next_lon
        lat = next_lat
    return tiles


def fetch_opencellid_tile_rows(token: str, bbox: tuple[float, float, float, float], page_limit: int, pause_seconds: float) -> pd.DataFrame:
    tile_frames: list[pd.DataFrame] = []
    offset = 0
    while True:
        params = {
            "key": token,
            "BBOX": ",".join(f"{value:.6f}" for value in bbox),
            "format": "csv",
            "limit": page_limit,
            "offset": offset,
        }
        response = requests.get("https://opencellid.org/cell/getInArea", params=params, timeout=120)
        response.raise_for_status()
        tile_df = parse_opencellid_area_csv(response.text)
        if tile_df.empty:
            break
        tile_frames.append(tile_df)
        if len(tile_df) < page_limit:
            break
        offset += page_limit
        if pause_seconds > 0:
            time.sleep(pause_seconds)
    if not tile_frames:
        return pd.DataFrame()
    return pd.concat(tile_frames, ignore_index=True)


def parse_opencellid_area_csv(text: str) -> pd.DataFrame:
    stripped = text.strip()
    if not stripped:
        return pd.DataFrame()
    if stripped.startswith("info,code"):
        return pd.DataFrame()
    return pd.read_csv(io.StringIO(stripped))


def prepare_towers(
    towers_df: pd.DataFrame,
    provider_mapping: dict[str, str],
    legacy_provider_policy: dict[str, str],
    current_operators: list[str],
) -> pd.DataFrame:
    towers = towers_df.copy()
    validate_columns(towers, ["mcc", "mnc", "latitude", "longitude"], "OpenCellID towers")

    towers["latitude"] = pd.to_numeric(towers["latitude"], errors="coerce")
    towers["longitude"] = pd.to_numeric(towers["longitude"], errors="coerce")
    if "average_signal" in towers.columns:
        towers["average_signal"] = pd.to_numeric(towers["average_signal"], errors="coerce")
    towers = towers.dropna(subset=["latitude", "longitude"])
    towers = towers[(towers["mcc"].astype(str).isin(["404", "405", "406"])) | (towers.get("country", "") == "India")]

    operator_series = towers["operator"] if "operator" in towers.columns else pd.Series("", index=towers.index)
    towers["provider"] = [
        provider_from_codes(mcc, mnc, operator_name, provider_mapping)
        for mcc, mnc, operator_name in zip(towers["mcc"], towers["mnc"], operator_series, strict=False)
    ]
    provider_policy = {
        normalize_provider_name(key): str(value).strip()
        for key, value in legacy_provider_policy.items()
    }
    normalized_current_operators = {normalize_provider_name(name) for name in current_operators}
    towers["provider"] = towers["provider"].map(lambda value: apply_provider_policy(value, provider_policy))
    towers = towers[towers["provider"].isin(normalized_current_operators)]
    return towers


def apply_provider_policy(provider: Any, config_provider_policy: dict[str, str]) -> str:
    normalized = normalize_provider_name(provider)
    action = config_provider_policy.get(normalized)
    if action == "suppress":
        return "Suppressed"
    if isinstance(action, str) and action:
        return normalize_provider_name(action)
    return normalized


def estimate_projected_crs(villages_gdf: gpd.GeoDataFrame) -> CRS:
    utm_crs = villages_gdf.estimate_utm_crs()
    return CRS.from_user_input(utm_crs or "EPSG:3857")


def find_towers_within_radius(
    villages_gdf: gpd.GeoDataFrame,
    towers_gdf: gpd.GeoDataFrame,
    radius_km: float,
) -> gpd.GeoDataFrame:
    if villages_gdf.empty:
        raise ValueError("No village coordinates are available after geocoding; cannot calculate tower distances.")
    if towers_gdf.empty:
        return gpd.GeoDataFrame(columns=["village_id", "provider", "distance_km"], geometry=[], crs="EPSG:4326")

    projected_crs = estimate_projected_crs(villages_gdf)
    villages_proj = villages_gdf.to_crs(projected_crs)
    towers_proj = towers_gdf.to_crs(projected_crs)

    village_points = villages_proj[["village_id", "geometry"]].rename(columns={"geometry": "village_geometry"})
    village_buffers = villages_proj[["village_id", "geometry"]].copy()
    village_buffers["geometry"] = village_buffers.geometry.buffer(radius_km * 1000)

    joined = gpd.sjoin(
        towers_proj,
        village_buffers,
        how="inner",
        predicate="within",
    ).rename(columns={"index_right": "village_buffer_index"})
    joined = joined.merge(village_points, on="village_id", how="left")
    joined["distance_km"] = joined.geometry.distance(joined["village_geometry"]) / 1000
    return joined


def summarize_provider_distances(nearby_towers: gpd.GeoDataFrame) -> pd.DataFrame:
    if nearby_towers.empty:
        return pd.DataFrame(columns=["village_id", "provider", "tower_count", "nearest_tower_km"])

    summary = (
        nearby_towers.groupby(["village_id", "provider"], dropna=False)
        .agg(
            tower_count=("provider", "size"),
            nearest_tower_km=("distance_km", "min"),
            strongest_signal_dbm=("average_signal", "max"),
        )
        .reset_index()
    )
    return summary


def join_ookla(
    villages_gdf: gpd.GeoDataFrame,
    ookla_path: Path,
    ookla_columns: dict[str, str],
    max_nearest_tile_km: float,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    empty_village = pd.DataFrame(
        columns=[
            "village_id",
            "village_ookla_download_mbps",
            "village_ookla_upload_mbps",
            "village_ookla_tests",
            "village_ookla_distance_km",
        ]
    )
    empty_provider = pd.DataFrame(columns=["village_id", "provider", "provider_ookla_download_mbps", "provider_ookla_upload_mbps", "provider_ookla_tests"])
    if not ookla_path.exists():
        return empty_village, empty_provider

    ookla_gdf = load_ookla_geodataframe(ookla_path, ookla_columns)
    if ookla_gdf.empty:
        return empty_village, empty_provider

    villages_4326 = villages_gdf.to_crs("EPSG:4326")
    ookla_4326 = ookla_gdf.to_crs("EPSG:4326")
    joined = gpd.sjoin(villages_4326, ookla_4326, how="left", predicate="within")
    village_level = pd.DataFrame()
    if not joined.empty and joined["index_right"].notna().any():
        village_level = (
            joined.dropna(subset=["index_right"])
            .groupby("village_id", dropna=False)
            .agg(
                village_ookla_download_mbps=("avg_download_mbps", "mean"),
                village_ookla_upload_mbps=("avg_upload_mbps", "mean"),
                village_ookla_tests=("tests", "sum"),
            )
            .reset_index()
        )
        village_level["village_ookla_distance_km"] = 0.0

    covered_ids = set(village_level["village_id"].tolist()) if not village_level.empty else set()
    missing_villages = villages_4326[~villages_4326["village_id"].isin(covered_ids)].copy()
    nearest_village_level = pd.DataFrame()
    if not missing_villages.empty:
        projected_crs = estimate_projected_crs(villages_gdf)
        nearest_join = gpd.sjoin_nearest(
            missing_villages.to_crs(projected_crs),
            ookla_4326.to_crs(projected_crs),
            how="left",
            distance_col="distance_m",
        )
        nearest_join = nearest_join[nearest_join["distance_m"].notna()].copy()
        nearest_join["distance_km"] = nearest_join["distance_m"] / 1000.0
        nearest_join = nearest_join[nearest_join["distance_km"] <= max_nearest_tile_km]
        if not nearest_join.empty:
            nearest_village_level = (
                nearest_join.groupby("village_id", dropna=False)
                .agg(
                    village_ookla_download_mbps=("avg_download_mbps", "mean"),
                    village_ookla_upload_mbps=("avg_upload_mbps", "mean"),
                    village_ookla_tests=("tests", "sum"),
                    village_ookla_distance_km=("distance_km", "min"),
                )
                .reset_index()
            )

    if village_level.empty:
        village_level = nearest_village_level if not nearest_village_level.empty else empty_village
    elif not nearest_village_level.empty:
        village_level = pd.concat([village_level, nearest_village_level], ignore_index=True)

    provider_level = empty_provider
    if "provider" in joined.columns:
        provider_level = (
            joined.dropna(subset=["provider"])
            .groupby(["village_id", "provider"], dropna=False)
            .agg(
                provider_ookla_download_mbps=("avg_download_mbps", "mean"),
                provider_ookla_upload_mbps=("avg_upload_mbps", "mean"),
                provider_ookla_tests=("tests", "sum"),
            )
            .reset_index()
        )
        provider_level["provider"] = provider_level["provider"].map(normalize_provider_name)

    return village_level, provider_level


def load_ookla_geodataframe(ookla_path: Path, ookla_columns: dict[str, str]) -> gpd.GeoDataFrame:
    suffix = ookla_path.suffix.lower()
    if suffix in {".geojson", ".gpkg", ".shp"}:
        gdf = gpd.read_file(ookla_path)
    elif suffix == ".parquet":
        gdf = gpd.read_parquet(ookla_path)
    elif suffix in {".csv", ".xlsx", ".xls"}:
        table = load_table(ookla_path)
        table = standardize_columns(table, ookla_columns)
        if "geometry" in table.columns:
            gdf = gpd.GeoDataFrame(table, geometry=gpd.GeoSeries.from_wkt(table["geometry"]), crs="EPSG:4326")
        elif {"latitude", "longitude"}.issubset(table.columns):
            gdf = to_points_gdf(table, "longitude", "latitude")
        else:
            raise ValueError("Ookla tabular input requires either a geometry column or latitude/longitude columns.")
    else:
        raise ValueError(f"Unsupported Ookla file format: {ookla_path}")

    if "provider" not in gdf.columns and ookla_columns.get("provider") in gdf.columns:
        gdf = standardize_columns(gdf, ookla_columns)
    else:
        gdf = gdf.rename(
            columns={
                ookla_columns.get("avg_download_mbps", "avg_download_mbps"): "avg_download_mbps",
                ookla_columns.get("avg_upload_mbps", "avg_upload_mbps"): "avg_upload_mbps",
                ookla_columns.get("tests", "tests"): "tests",
            }
        )

    if "provider" in gdf.columns:
        gdf["provider"] = gdf["provider"].map(normalize_provider_name)

    for column in ["avg_download_mbps", "avg_upload_mbps", "tests"]:
        if column in gdf.columns:
            gdf[column] = pd.to_numeric(gdf[column], errors="coerce")
    return gdf


def build_provider_rows(
    villages_df: pd.DataFrame,
    provider_distances: pd.DataFrame,
    village_level_ookla: pd.DataFrame,
    provider_level_ookla: pd.DataFrame,
    config: dict[str, Any],
    source_recency: dict[str, str] | None = None,
) -> pd.DataFrame:
    current_operators = [normalize_provider_name(name) for name in config.get("current_operators", [])]
    legacy_provider_policy = {
        normalize_provider_name(key): str(value).strip()
        for key, value in config.get("legacy_provider_policy", {}).items()
    }

    provider_distances = provider_distances.copy()
    if not provider_distances.empty:
        provider_distances["provider"] = provider_distances["provider"].map(
            lambda value: apply_provider_policy(value, legacy_provider_policy)
        )
        provider_distances = provider_distances[provider_distances["provider"].isin(current_operators)]
        provider_distances = (
            provider_distances.groupby(["village_id", "provider"], dropna=False)
            .agg(
                tower_count=("tower_count", "sum"),
                nearest_tower_km=("nearest_tower_km", "min"),
                strongest_signal_dbm=("strongest_signal_dbm", "max"),
            )
            .reset_index()
        )

    provider_level_ookla = provider_level_ookla.copy()
    if not provider_level_ookla.empty:
        provider_level_ookla["provider"] = provider_level_ookla["provider"].map(
            lambda value: apply_provider_policy(value, legacy_provider_policy)
        )
        provider_level_ookla = provider_level_ookla[provider_level_ookla["provider"].isin(current_operators)]
        provider_level_ookla = (
            provider_level_ookla.groupby(["village_id", "provider"], dropna=False)
            .agg(
                provider_ookla_download_mbps=("provider_ookla_download_mbps", "mean"),
                provider_ookla_upload_mbps=("provider_ookla_upload_mbps", "mean"),
                provider_ookla_tests=("provider_ookla_tests", "sum"),
            )
            .reset_index()
        )

    known_providers = current_operators

    village_provider_index = pd.MultiIndex.from_product(
        [villages_df["village_id"].tolist(), known_providers],
        names=["village_id", "provider"],
    ).to_frame(index=False)

    output = village_provider_index.merge(villages_df, on="village_id", how="left")
    output = output.merge(provider_distances, on=["village_id", "provider"], how="left")
    output = output.merge(village_level_ookla, on="village_id", how="left")
    output = output.merge(provider_level_ookla, on=["village_id", "provider"], how="left")
    if source_recency:
        for key, value in source_recency.items():
            output[key] = value

    output["coverage_score"] = output.apply(lambda row: classify_coverage(row, config), axis=1)
    output["assessment_note"] = output.apply(build_note, axis=1)
    output = output.sort_values(["state", "district", "block", "village", "provider"]).reset_index(drop=True)
    return output


def classify_coverage(row: pd.Series, config: dict[str, Any]) -> str:
    nearest = row.get("nearest_tower_km")
    strongest_signal = row.get("strongest_signal_dbm")
    village_ookla = row.get("village_ookla_download_mbps")
    provider_ookla = row.get("provider_ookla_download_mbps")
    village_ookla_distance = row.get("village_ookla_distance_km")

    if pd.notna(nearest):
        nearest = float(nearest)
        strong_signal_dbm = float(config["scoring"].get("strong_signal_dbm", -85))
        moderate_signal_dbm = float(config["scoring"].get("moderate_signal_dbm", -100))
        if nearest <= float(config["distance"]["strong_km"]) and (pd.isna(strongest_signal) or float(strongest_signal) >= strong_signal_dbm):
            return "Strong"
        if nearest <= float(config["distance"]["moderate_km"]) and (pd.isna(strongest_signal) or float(strongest_signal) >= moderate_signal_dbm):
            return "Moderate"
        if nearest <= float(config["distance"]["search_radius_km"]):
            return "Weak"

    benchmark = provider_ookla if pd.notna(provider_ookla) else village_ookla
    if pd.notna(benchmark):
        within_ookla_limit = pd.isna(village_ookla_distance) or float(village_ookla_distance) <= float(config.get("ookla", {}).get("nearest_tile_max_km", 5))
        if within_ookla_limit and float(benchmark) >= float(config["scoring"]["strong_download_mbps"]):
            return "Moderate"
        if within_ookla_limit and float(benchmark) >= float(config["scoring"]["moderate_download_mbps"]):
            return "Weak"

    return "Unknown"


def build_note(row: pd.Series) -> str:
    parts: list[str] = []
    if pd.notna(row.get("nearest_tower_km")):
        parts.append(f"nearest tower {float(row['nearest_tower_km']):.2f} km")
    if pd.notna(row.get("tower_count")):
        parts.append(f"{int(row['tower_count'])} tower(s) within radius")
    if pd.notna(row.get("strongest_signal_dbm")):
        parts.append(f"best tower signal {float(row['strongest_signal_dbm']):.0f} dBm")
    if pd.notna(row.get("provider_ookla_download_mbps")):
        parts.append(f"provider Ookla {float(row['provider_ookla_download_mbps']):.1f} Mbps")
    elif pd.notna(row.get("village_ookla_download_mbps")):
        parts.append(f"village Ookla {float(row['village_ookla_download_mbps']):.1f} Mbps")
    if pd.notna(row.get("village_ookla_distance_km")) and float(row["village_ookla_distance_km"]) > 0:
        parts.append(f"nearest Ookla tile {float(row['village_ookla_distance_km']):.2f} km")
    return "; ".join(parts) if parts else "No tower or Ookla evidence available."


def write_summary_workbook(provider_rows: pd.DataFrame, summary_xlsx: Path) -> Path:
    metadata_columns = [
        column
        for column in ["opencellid_data_as_of", "opencellid_recency_basis", "ookla_data_as_of", "ookla_recency_basis", "data_as_of", "pipeline_generated_at_utc"]
        if column in provider_rows.columns
    ]
    village_summary = (
        provider_rows.pivot_table(
            index=["village_id", "state", "district", "block", "village", "lgd_code", "latitude", "longitude", "coordinate_source", *metadata_columns],
            columns="provider",
            values="coverage_score",
            aggfunc="first",
        )
        .reset_index()
    )

    score_summary = (
        provider_rows.groupby(["provider", "coverage_score"], dropna=False)
        .size()
        .reset_index(name="village_count")
        .sort_values(["provider", "coverage_score"])
    )

    target_path = summary_xlsx
    try:
        write_summary_workbook_file(provider_rows, village_summary, score_summary, target_path)
    except PermissionError:
        target_path = summary_xlsx.with_name(f"{summary_xlsx.stem}_latest{summary_xlsx.suffix}")
        write_summary_workbook_file(provider_rows, village_summary, score_summary, target_path)
        print(f"Workbook target was locked, wrote fallback summary to {target_path}")
    return target_path


def write_summary_workbook_file(
    provider_rows: pd.DataFrame,
    village_summary: pd.DataFrame,
    score_summary: pd.DataFrame,
    target_path: Path,
) -> None:
    with pd.ExcelWriter(target_path, engine="openpyxl") as writer:
        provider_rows.to_excel(writer, sheet_name="provider_estimates", index=False)
        village_summary.to_excel(writer, sheet_name="village_summary", index=False)
        score_summary.to_excel(writer, sheet_name="score_summary", index=False)
    autosize_workbook(target_path)


def autosize_workbook(path: Path) -> None:
    workbook = load_workbook(path)
    for worksheet in workbook.worksheets:
        for column_cells in worksheet.columns:
            width = max(len(str(cell.value or "")) for cell in column_cells[: min(len(column_cells), 100)])
            worksheet.column_dimensions[column_cells[0].column_letter].width = min(max(width + 2, 12), 42)
    workbook.save(path)


def write_geojson(villages_gdf: gpd.GeoDataFrame, provider_rows: pd.DataFrame, output_path: Path) -> None:
    score_pivot = provider_rows.pivot_table(
        index="village_id",
        columns="provider",
        values="coverage_score",
        aggfunc="first",
    ).reset_index()
    distance_pivot = provider_rows.pivot_table(
        index="village_id",
        columns="provider",
        values="nearest_tower_km",
        aggfunc="first",
    ).reset_index()

    score_pivot.columns = ["village_id", *[f"{slugify(column)}_score" for column in score_pivot.columns[1:]]]
    distance_pivot.columns = ["village_id", *[f"{slugify(column)}_nearest_km" for column in distance_pivot.columns[1:]]]
    metadata_columns = [
        column
        for column in ["opencellid_data_as_of", "opencellid_recency_basis", "ookla_data_as_of", "ookla_recency_basis", "data_as_of", "pipeline_generated_at_utc"]
        if column in provider_rows.columns
    ]
    metadata = provider_rows[["village_id", *metadata_columns]].drop_duplicates(subset=["village_id"]) if metadata_columns else pd.DataFrame(columns=["village_id"])

    export_gdf = (
        villages_gdf.merge(score_pivot, on="village_id", how="left")
        .merge(distance_pivot, on="village_id", how="left")
        .merge(metadata, on="village_id", how="left")
    )
    export_gdf.to_file(output_path, driver="GeoJSON")


def slugify(value: str) -> str:
    return "".join(char.lower() if char.isalnum() else "_" for char in value).strip("_")
