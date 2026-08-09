from __future__ import annotations

import json
import os
from pathlib import Path

import pandas as pd

from archive_utils import archive_existing_path
from enrich_requested_districts_from_stoptb import DISTRICT_OUTPUT_ROOT, SITE_ROOT
from source_metadata import file_mtime_iso


ROOT = Path(__file__).resolve().parent
STATUS_CSV = DISTRICT_OUTPUT_ROOT / "requested_district_status.csv"
SUMMARY_CSV = DISTRICT_OUTPUT_ROOT / "requested_district_summary.csv"
SUMMARY_JSON = DISTRICT_OUTPUT_ROOT / "requested_district_summary.json"


def confidence_level_for_status(status: str) -> str:
    if status == "completed_tower_enriched":
        return "high"
    if status == "fallback_tower_enriched":
        return "medium"
    if status in {"fetched_no_tower_evidence", "fallback_no_tower_evidence"}:
        return "low"
    if status == "source_missing":
        return "none"
    return "pending"


def confidence_level_for_source_mode(source_mode: str) -> str:
    if source_mode in {"completed_tower_enriched", "shared_tower_pool_enriched"}:
        return "high"
    if source_mode == "fallback_tower_enriched":
        return "medium"
    if source_mode in {"fetched_no_tower_evidence", "fallback_no_tower_evidence"}:
        return "low"
    if source_mode == "source_missing":
        return "none"
    return "pending"


def first_non_empty(series: pd.Series | None) -> str:
    if series is None:
        return ""
    values = series.fillna("").astype(str).map(str.strip)
    values = values[values != ""]
    return str(values.iloc[0]) if not values.empty else ""


def district_recency(output_dir: Path, provider_csv: Path, matched_state: str) -> dict[str, str]:
    recency = {
        "opencellid_data_as_of": "",
        "opencellid_recency_basis": "",
        "ookla_data_as_of": "",
        "ookla_recency_basis": "",
        "data_as_of": "",
    }
    if provider_csv.exists():
        provider_df = pd.read_csv(provider_csv, low_memory=False)
        for field in ["opencellid_data_as_of", "opencellid_recency_basis", "ookla_data_as_of", "ookla_recency_basis", "data_as_of"]:
            if field in provider_df.columns:
                recency[field] = first_non_empty(provider_df[field])

    if not recency["opencellid_data_as_of"]:
        recency["opencellid_data_as_of"] = file_mtime_iso(output_dir / "data" / "opencellid_district.csv.gz")
        recency["opencellid_recency_basis"] = "district_file_mtime" if recency["opencellid_data_as_of"] else ""

    if not recency["ookla_data_as_of"]:
        state_cache = DISTRICT_OUTPUT_ROOT / "_state_cache" / "".join(ch.lower() if ch.isalnum() else "_" for ch in matched_state).strip("_") / "ookla_state.geojson"
        recency["ookla_data_as_of"] = file_mtime_iso(state_cache) or file_mtime_iso(output_dir / "data" / "ookla_state.geojson")
        recency["ookla_recency_basis"] = "state_cache_mtime" if recency["ookla_data_as_of"] else ""

    if not recency["data_as_of"]:
        recency["data_as_of"] = recency["opencellid_data_as_of"] or recency["ookla_data_as_of"]
    return recency


def main() -> None:
    status_df = pd.read_csv(STATUS_CSV)
    present = status_df[status_df["status"] != "source_missing"].copy()

    rows: list[dict[str, object]] = []
    for row in present.itertuples(index=False):
        output_dir = Path(str(row.output_dir))
        village_csv = output_dir / "outputs" / "village_provider_signal_estimate.csv"
        if not village_csv.exists():
            continue
        provider_df = pd.read_csv(village_csv, low_memory=False)
        village_count = int(provider_df["village_id"].nunique()) if "village_id" in provider_df.columns else int(
            provider_df[["state", "district", "block", "village"]].drop_duplicates().shape[0]
        )
        gps_village_count = int(
            provider_df.dropna(subset=["latitude", "longitude"])["village_id"].nunique()
        ) if {"latitude", "longitude", "village_id"}.issubset(provider_df.columns) else 0
        row_count = int(getattr(row, "source_row_count"))
        rows.append(
            {
                "state": row.requested_state,
                "district": row.requested_district,
                "matched_state": row.matched_state,
                "matched_district": row.matched_district,
                "source_mode": getattr(row, "status"),
                "confidence_level": confidence_level_for_source_mode(str(getattr(row, "status"))),
                "row_count": row_count,
                "village_count": village_count,
                "gps_village_count": gps_village_count,
                **district_recency(output_dir, village_csv, row.matched_state),
                "relative_map_html": os.path.relpath(output_dir / "outputs" / "village_connectivity_map.html", SITE_ROOT).replace("\\", "/"),
                "relative_provider_csv": os.path.relpath(village_csv, SITE_ROOT).replace("\\", "/"),
            }
        )

    summary_df = pd.DataFrame(rows).sort_values(["state", "district"]).reset_index(drop=True)
    archive_existing_path(SUMMARY_CSV)
    summary_df.to_csv(SUMMARY_CSV, index=False)

    payload = {
        "requested_district_count": int(len(status_df)),
        "present_district_count": int(len(summary_df)),
        "processing_mode": "gps_plus_opencellid_ookla",
        "missing_districts": [
            {"state": row.requested_state, "district": row.requested_district}
            for row in status_df[status_df["status"] == "source_missing"].itertuples(index=False)
        ],
    }
    archive_existing_path(SUMMARY_JSON)
    SUMMARY_JSON.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Wrote {SUMMARY_CSV}")
    print(f"Wrote {SUMMARY_JSON}")


if __name__ == "__main__":
    main()
