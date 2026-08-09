from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import pandas as pd


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
MASTER_XLSX = DATA_DIR / "lgd_village_master.xlsx"
OOKLA_PARQUET = DATA_DIR / "ookla_mobile_tiles_q1_2026.parquet"
OOKLA_GEOJSON = DATA_DIR / "ookla_mobile_tiles.geojson"


def main() -> None:
    villages = pd.read_excel(MASTER_XLSX)
    min_lat = float(villages["latitude"].min()) - 0.15
    max_lat = float(villages["latitude"].max()) + 0.15
    min_lon = float(villages["longitude"].min()) - 0.15
    max_lon = float(villages["longitude"].max()) + 0.15

    parquet = pd.read_parquet(
        OOKLA_PARQUET,
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
    gdf.to_file(OOKLA_GEOJSON, driver="GeoJSON")
    print(f"Wrote {len(gdf)} Ookla mobile tiles to {OOKLA_GEOJSON}")


if __name__ == "__main__":
    main()
