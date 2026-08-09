from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from archive_utils import archive_existing_path
from enrich_requested_districts_from_stoptb import (
    DISTRICT_OUTPUT_ROOT,
    REQUESTED_DISTRICTS_CSV,
    SOURCE_CSV,
    canonicalize_district,
    canonicalize_state,
    district_key,
    existing_district_dir_candidates,
    load_requested_districts,
    normalize_text,
    provider_csv_has_tower_evidence,
)
from source_metadata import file_mtime_iso


ROOT = Path(__file__).resolve().parent
STATUS_CSV = DISTRICT_OUTPUT_ROOT / "requested_district_status.csv"
STATUS_MD = DISTRICT_OUTPUT_ROOT / "requested_district_status.md"
STATUS_JSON = DISTRICT_OUTPUT_ROOT / "requested_district_status.json"


def build_source_presence(source_df: pd.DataFrame) -> tuple[dict[tuple[str, str], int], dict[tuple[str, str], int]]:
    row_counts = (
        source_df.groupby(["canonical_state", "canonical_district"], dropna=False)
        .size()
        .to_dict()
    )
    village_counts = (
        source_df.assign(
            village_clean=source_df["beneficiary_info-village"].fillna("").astype(str).str.strip()
        )
        .loc[lambda df: df["village_clean"] != ""]
        .groupby(["canonical_state", "canonical_district"], dropna=False)["village_clean"]
        .nunique()
        .to_dict()
    )
    return row_counts, village_counts


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


def first_non_empty(series: pd.Series | None) -> str:
    if series is None:
        return ""
    values = series.fillna("").astype(str).map(str.strip)
    values = values[values != ""]
    return str(values.iloc[0]) if not values.empty else ""


def district_recency(output_dir: Path, matched_state: str, provider_csv: Path | None = None) -> dict[str, str]:
    provider_csv = provider_csv or (output_dir / "outputs" / "village_provider_signal_estimate.csv")
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
        state_asset = DISTRICT_OUTPUT_ROOT / "_state_cache" / "".join(ch.lower() if ch.isalnum() else "_" for ch in matched_state).strip("_") / "ookla_state.geojson"
        recency["ookla_data_as_of"] = file_mtime_iso(state_asset) or file_mtime_iso(output_dir / "data" / "ookla_state.geojson")
        recency["ookla_recency_basis"] = "state_cache_mtime" if recency["ookla_data_as_of"] else ""

    if not recency["data_as_of"]:
        recency["data_as_of"] = recency["opencellid_data_as_of"] or recency["ookla_data_as_of"]
    return recency


def district_status_rows() -> pd.DataFrame:
    requested = load_requested_districts()
    source_df = pd.read_csv(SOURCE_CSV, low_memory=False)
    source_df["canonical_state"] = source_df["login_info-state_name"].map(canonicalize_state)
    source_df["canonical_district"] = source_df["login_info-district_name"].map(canonicalize_district)

    row_counts, village_counts = build_source_presence(source_df)

    rows: list[dict[str, object]] = []
    for item in requested:
        requested_state = item["requested_state"]
        requested_district = item["requested_district"]
        matched_state = item["matched_state"]
        matched_district = item["matched_district"]
        key = (matched_state, matched_district)
        matched_key_norm = district_key(matched_state, matched_district)
        has_source_rows = key in row_counts
        source_row_count = int(row_counts.get(key, 0))
        source_village_count = int(village_counts.get(key, 0))

        candidate_dirs = existing_district_dir_candidates(requested_state, requested_district, matched_state, matched_district)
        chosen_dir = None
        has_tower_enrichment = False
        has_opencellid_file = False
        opencellid_file_size = 0
        for candidate_dir in candidate_dirs:
            tower_file = candidate_dir / "data" / "opencellid_district.csv.gz"
            if tower_file.exists():
                has_opencellid_file = True
                opencellid_file_size = max(opencellid_file_size, int(tower_file.stat().st_size))
            provider_csv = candidate_dir / "outputs" / "village_provider_signal_estimate.csv"
            if provider_csv_has_tower_evidence(provider_csv):
                chosen_dir = candidate_dir
                has_tower_enrichment = True
                break
            if chosen_dir is None and provider_csv.exists():
                chosen_dir = candidate_dir

        if has_tower_enrichment:
            status = "completed_tower_enriched" if has_source_rows else "fallback_tower_enriched"
        elif has_opencellid_file and has_source_rows:
            status = "fetched_no_tower_evidence"
        elif has_opencellid_file and not has_source_rows:
            status = "fallback_no_tower_evidence"
        elif has_source_rows:
            status = "pending_fetch"
        else:
            status = "source_missing"

        rows.append(
            {
                "requested_state": requested_state,
                "requested_district": requested_district,
                "matched_state": matched_state,
                "matched_district": matched_district,
                "matched_key_normalized": f"{matched_key_norm[0]} | {matched_key_norm[1]}",
                "status": status,
                "confidence_level": confidence_level_for_status(status),
                "source_row_count": source_row_count,
                "source_village_count": source_village_count,
                "has_tower_enrichment": has_tower_enrichment,
                "has_opencellid_file": has_opencellid_file,
                "opencellid_file_size_bytes": opencellid_file_size,
                "output_dir": str(chosen_dir.resolve()) if chosen_dir else "",
                **(
                    district_recency(chosen_dir or candidate_dirs[0], matched_state)
                    if candidate_dirs
                    else {
                        "opencellid_data_as_of": "",
                        "opencellid_recency_basis": "",
                        "ookla_data_as_of": "",
                        "ookla_recency_basis": "",
                        "data_as_of": "",
                    }
                ),
            }
        )

    return pd.DataFrame(rows).sort_values(["status", "requested_state", "requested_district"]).reset_index(drop=True)


def write_markdown(df: pd.DataFrame) -> None:
    counts = df["status"].value_counts().to_dict()
    lines = [
        "# Requested District Status",
        "",
        f"- Requested districts: {len(df)}",
        f"- Completed with tower-backed enrichment: {counts.get('completed_tower_enriched', 0)}",
        f"- Completed with LGD fallback plus tower-backed enrichment: {counts.get('fallback_tower_enriched', 0)}",
        f"- Completed fetch with no tower evidence used: {counts.get('fetched_no_tower_evidence', 0)}",
        f"- Completed with LGD fallback and no tower evidence used: {counts.get('fallback_no_tower_evidence', 0)}",
        f"- Pending fetch from current source: {counts.get('pending_fetch', 0)}",
        f"- Source missing: {counts.get('source_missing', 0)}",
        "",
        "## District Table",
        "",
        "| Requested State | Requested District | Matched State | Matched District | Status | Confidence | Source Rows | Source Villages | OpenCellID As Of | Ookla As Of | Output Dir |",
        "|---|---|---|---|---|---|---:|---:|---|---|---|",
    ]
    for row in df.itertuples(index=False):
        lines.append(
            f"| {row.requested_state} | {row.requested_district} | {row.matched_state} | {row.matched_district} | "
            f"{row.status} | {row.confidence_level} | {row.source_row_count} | {row.source_village_count} | {getattr(row, 'opencellid_data_as_of', '')} | {getattr(row, 'ookla_data_as_of', '')} | {row.output_dir} |"
        )
    archive_existing_path(STATUS_MD)
    STATUS_MD.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    df = district_status_rows()
    STATUS_CSV.parent.mkdir(parents=True, exist_ok=True)
    archive_existing_path(STATUS_CSV)
    df.to_csv(STATUS_CSV, index=False)
    write_markdown(df)
    payload = {
        "requested_district_count": int(len(df)),
        "status_counts": {key: int(value) for key, value in df["status"].value_counts().to_dict().items()},
        "requested_district_source": str(REQUESTED_DISTRICTS_CSV),
        "source_screening_file": str(SOURCE_CSV),
    }
    archive_existing_path(STATUS_JSON)
    STATUS_JSON.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Wrote {STATUS_CSV}")
    print(f"Wrote {STATUS_MD}")
    print(f"Wrote {STATUS_JSON}")


if __name__ == "__main__":
    main()
