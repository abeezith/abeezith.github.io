from __future__ import annotations

import argparse
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import yaml


ROOT = Path(__file__).resolve().parent
RETENTION_ROOT = ROOT / "outputs" / "retention"
WEEKLY_ROOT = RETENTION_ROOT / "weekly"
MONTHLY_ROOT = RETENTION_ROOT / "monthly"
ANNUAL_ROOT = RETENTION_ROOT / "annual"

RAW_OUTPUT_FILES = {
    "village_provider_signal_estimate.csv": ROOT / "outputs" / "village_provider_signal_estimate.csv",
    "village_connectivity_summary.xlsx": ROOT / "outputs" / "village_connectivity_summary.xlsx",
    "village_connectivity.geojson": ROOT / "outputs" / "village_connectivity.geojson",
    "requested_district_status.csv": ROOT / "outputs" / "requested_districts" / "requested_district_status.csv",
    "requested_district_summary.csv": ROOT / "outputs" / "requested_districts" / "requested_district_summary.csv",
    "requested_district_status.json": ROOT / "outputs" / "requested_districts" / "requested_district_status.json",
    "requested_district_status.md": ROOT / "outputs" / "requested_districts" / "requested_district_status.md",
}


def load_config(config_path: Path) -> dict[str, Any]:
    if not config_path.exists():
        return {}
    with config_path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def retention_config(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "enabled": bool(config.get("retention", {}).get("enabled", True)),
        "weekly_days": int(config.get("retention", {}).get("weekly_days", 28)),
        "monthly_months": int(config.get("retention", {}).get("monthly_months", 12)),
        "annual_enabled": bool(config.get("retention", {}).get("annual_enabled", True)),
    }


def copy_current_outputs(weekly_dir: Path) -> list[Path]:
    weekly_dir.mkdir(parents=True, exist_ok=True)
    copied: list[Path] = []
    for name, source in RAW_OUTPUT_FILES.items():
        if not source.exists():
            continue
        target = weekly_dir / name
        shutil.copy2(source, target)
        copied.append(target)

    manifest = {
        "captured_at_utc": datetime.now(tz=UTC).isoformat().replace("+00:00", "Z"),
        "sources": [path.name for path in copied],
    }
    (weekly_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return copied


def load_provider_frames(weekly_dirs: list[Path]) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for weekly_dir in weekly_dirs:
        provider_csv = weekly_dir / "village_provider_signal_estimate.csv"
        manifest_path = weekly_dir / "manifest.json"
        if not provider_csv.exists():
            continue
        frame = pd.read_csv(provider_csv, low_memory=False)
        frame["snapshot_bucket"] = weekly_dir.name
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            frame["snapshot_captured_at_utc"] = str(manifest.get("captured_at_utc") or "")
        else:
            frame["snapshot_captured_at_utc"] = ""
        frames.append(frame)
    if not frames:
        return pd.DataFrame()
    combined = pd.concat(frames, ignore_index=True)
    combined["snapshot_captured_at_utc"] = pd.to_datetime(combined["snapshot_captured_at_utc"], errors="coerce", utc=True)
    return combined


def coverage_rank(series: pd.Series) -> pd.Series:
    mapping = {"Unknown": 0, "Weak": 1, "Moderate": 2, "Strong": 3}
    return series.fillna("Unknown").astype(str).map(mapping).fillna(0)


def mean_or_empty(series: pd.Series) -> float | None:
    values = pd.to_numeric(series, errors="coerce").dropna()
    return float(values.mean()) if not values.empty else None


def weighted_mean(frame: pd.DataFrame, value_column: str, weight_column: str) -> float | None:
    values = pd.to_numeric(frame[value_column], errors="coerce")
    weights = pd.to_numeric(frame[weight_column], errors="coerce").fillna(0)
    mask = values.notna() & weights.notna() & (weights > 0)
    if not mask.any():
        return None
    weighted_values = values[mask] * weights[mask]
    return float(weighted_values.sum() / weights[mask].sum())


def summarize_provider_group(group: pd.DataFrame, bucket_label: str, bucket_column: str) -> dict[str, Any]:
    latest = group.sort_values("snapshot_captured_at_utc").iloc[-1]
    total_rows = int(len(group))
    snapshot_count = int(group[bucket_column].nunique())
    village_count = int(group["village_id"].nunique()) if "village_id" in group.columns else 0
    rank_series = coverage_rank(group["coverage_score"])
    return {
        bucket_column: bucket_label,
        "state": str(latest.get("state", "")),
        "district": str(latest.get("district", "")),
        "provider": str(latest.get("provider", "")),
        "snapshot_count": snapshot_count,
        "row_count": total_rows,
        "village_count": village_count,
        "strong_pct": round(float((group["coverage_score"] == "Strong").mean() * 100), 2),
        "moderate_pct": round(float((group["coverage_score"] == "Moderate").mean() * 100), 2),
        "weak_pct": round(float((group["coverage_score"] == "Weak").mean() * 100), 2),
        "unknown_pct": round(float((group["coverage_score"] == "Unknown").mean() * 100), 2),
        "coverage_rank_mean": round(float(rank_series.mean()), 3),
        "nearest_tower_km_mean": mean_or_empty(group["nearest_tower_km"]),
        "nearest_tower_km_median": mean_or_empty(group["nearest_tower_km"].median().to_frame().T.iloc[0]) if False else float(pd.to_numeric(group["nearest_tower_km"], errors="coerce").dropna().median()) if pd.to_numeric(group["nearest_tower_km"], errors="coerce").dropna().size else None,
        "strongest_signal_dbm_mean": mean_or_empty(group["strongest_signal_dbm"]),
        "village_ookla_download_mbps_mean": mean_or_empty(group["village_ookla_download_mbps"]),
        "village_ookla_upload_mbps_mean": mean_or_empty(group["village_ookla_upload_mbps"]),
        "village_ookla_tests_mean": mean_or_empty(group["village_ookla_tests"]),
        "provider_ookla_download_mbps_mean": mean_or_empty(group["provider_ookla_download_mbps"]),
        "provider_ookla_upload_mbps_mean": mean_or_empty(group["provider_ookla_upload_mbps"]),
        "provider_ookla_tests_mean": mean_or_empty(group["provider_ookla_tests"]),
        "latest_snapshot_captured_at_utc": latest.get("snapshot_captured_at_utc").isoformat() if pd.notna(latest.get("snapshot_captured_at_utc")) else "",
        "latest_data_as_of": str(latest.get("data_as_of", "")),
        "latest_opencellid_data_as_of": str(latest.get("opencellid_data_as_of", "")),
        "latest_ookla_data_as_of": str(latest.get("ookla_data_as_of", "")),
    }


def build_monthly_rollup(weekly_dirs: list[Path], monthly_dir: Path) -> Path | None:
    frame = load_provider_frames(weekly_dirs)
    if frame.empty:
        return None

    frame["bucket_month"] = frame["snapshot_captured_at_utc"].dt.to_period("M").astype(str)
    bucket_label = monthly_dir.name
    current_month = frame[frame["bucket_month"] == bucket_label].copy()
    if current_month.empty:
        return None

    grouped = current_month.groupby(["state", "district", "provider"], dropna=False, sort=True)
    rows = [summarize_provider_group(group, bucket_label, "bucket_month") for _, group in grouped]
    monthly_frame = pd.DataFrame(rows).sort_values(["state", "district", "provider"]).reset_index(drop=True)
    monthly_dir.mkdir(parents=True, exist_ok=True)
    output_path = monthly_dir / "monthly_district_provider_rollup.csv"
    monthly_frame.to_csv(output_path, index=False)
    manifest = {
        "generated_at_utc": datetime.now(tz=UTC).isoformat().replace("+00:00", "Z"),
        "weekly_snapshot_count": int(current_month["snapshot_bucket"].nunique()),
        "row_count": int(len(current_month)),
        "bucket": bucket_label,
    }
    (monthly_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return output_path


def build_annual_rollup(monthly_root: Path, annual_dir: Path) -> Path | None:
    monthly_files = sorted(monthly_root.glob("*/monthly_district_provider_rollup.csv"))
    if not monthly_files:
        return None

    month_frames: list[pd.DataFrame] = []
    for file in monthly_files:
        frame = pd.read_csv(file, low_memory=False)
        frame["bucket_month"] = file.parent.name
        month_frames.append(frame)
    if not month_frames:
        return None

    combined = pd.concat(month_frames, ignore_index=True)
    current_year = annual_dir.name
    combined = combined[combined["bucket_month"].astype(str).str.startswith(current_year)].copy()
    if combined.empty:
        return None

    def aggregate(group: pd.DataFrame) -> pd.Series:
        weights = pd.to_numeric(group["snapshot_count"], errors="coerce").fillna(1)
        result = {
            "year": current_year,
            "state": str(group["state"].iloc[-1]),
            "district": str(group["district"].iloc[-1]),
            "provider": str(group["provider"].iloc[-1]),
            "month_count": int(group["bucket_month"].nunique()),
            "snapshot_count": int(pd.to_numeric(group["snapshot_count"], errors="coerce").fillna(0).sum()),
            "row_count": int(pd.to_numeric(group["row_count"], errors="coerce").fillna(0).sum()),
            "village_count": int(group["village_count"].max()),
            "strong_pct": round(float(weighted_mean(group, "strong_pct", "snapshot_count") or 0.0), 2),
            "moderate_pct": round(float(weighted_mean(group, "moderate_pct", "snapshot_count") or 0.0), 2),
            "weak_pct": round(float(weighted_mean(group, "weak_pct", "snapshot_count") or 0.0), 2),
            "unknown_pct": round(float(weighted_mean(group, "unknown_pct", "snapshot_count") or 0.0), 2),
            "coverage_rank_mean": round(float(weighted_mean(group, "coverage_rank_mean", "snapshot_count") or 0.0), 3),
            "nearest_tower_km_mean": weighted_mean(group, "nearest_tower_km_mean", "snapshot_count"),
            "nearest_tower_km_median": weighted_mean(group, "nearest_tower_km_median", "snapshot_count"),
            "strongest_signal_dbm_mean": weighted_mean(group, "strongest_signal_dbm_mean", "snapshot_count"),
            "village_ookla_download_mbps_mean": weighted_mean(group, "village_ookla_download_mbps_mean", "snapshot_count"),
            "village_ookla_upload_mbps_mean": weighted_mean(group, "village_ookla_upload_mbps_mean", "snapshot_count"),
            "village_ookla_tests_mean": weighted_mean(group, "village_ookla_tests_mean", "snapshot_count"),
            "provider_ookla_download_mbps_mean": weighted_mean(group, "provider_ookla_download_mbps_mean", "snapshot_count"),
            "provider_ookla_upload_mbps_mean": weighted_mean(group, "provider_ookla_upload_mbps_mean", "snapshot_count"),
            "provider_ookla_tests_mean": weighted_mean(group, "provider_ookla_tests_mean", "snapshot_count"),
            "latest_data_as_of": str(group.sort_values("bucket_month").iloc[-1].get("latest_data_as_of", "")),
            "latest_opencellid_data_as_of": str(group.sort_values("bucket_month").iloc[-1].get("latest_opencellid_data_as_of", "")),
            "latest_ookla_data_as_of": str(group.sort_values("bucket_month").iloc[-1].get("latest_ookla_data_as_of", "")),
        }
        return pd.Series(result)

    annual_frame = combined.groupby(["state", "district", "provider"], dropna=False, sort=True).apply(aggregate).reset_index(drop=True)
    annual_dir.mkdir(parents=True, exist_ok=True)
    output_path = annual_dir / "annual_district_provider_rollup.csv"
    annual_frame.to_csv(output_path, index=False)
    manifest = {
        "generated_at_utc": datetime.now(tz=UTC).isoformat().replace("+00:00", "Z"),
        "month_count": int(annual_frame["month_count"].max()) if not annual_frame.empty else 0,
        "bucket": current_year,
    }
    (annual_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return output_path


def prune_by_age(root: Path, keep_days: int | None = None, keep_months: int | None = None) -> int:
    removed = 0
    now = datetime.now(tz=UTC)
    for child in sorted(root.iterdir()) if root.exists() else []:
        if not child.is_dir():
            continue
        if keep_days is not None:
            manifest = child / "manifest.json"
            if manifest.exists():
                data = json.loads(manifest.read_text(encoding="utf-8"))
                captured = pd.to_datetime(data.get("captured_at_utc"), errors="coerce", utc=True)
                if pd.notna(captured) and (now - captured.to_pydatetime()).days > keep_days:
                    shutil.rmtree(child, ignore_errors=True)
                    removed += 1
        elif keep_months is not None:
            try:
                bucket = datetime.strptime(child.name, "%Y-%m")
            except ValueError:
                continue
            month_age = (now.year - bucket.year) * 12 + (now.month - bucket.month)
            if month_age >= keep_months:
                shutil.rmtree(child, ignore_errors=True)
                removed += 1
    return removed


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply weekly, monthly, and annual retention for connectivity outputs.")
    parser.add_argument("--config", default=ROOT / "config.yaml", type=Path, help="Path to the YAML config file.")
    args = parser.parse_args()

    config = load_config(args.config)
    settings = retention_config(config)
    if not settings["enabled"]:
        print("Retention is disabled in config.yaml.")
        return

    now = datetime.now(tz=UTC)
    week_bucket = now.strftime("%G-W%V")
    month_bucket = now.strftime("%Y-%m")
    year_bucket = now.strftime("%Y")

    weekly_dir = WEEKLY_ROOT / week_bucket
    if weekly_dir.exists():
        shutil.rmtree(weekly_dir, ignore_errors=True)
    copy_current_outputs(weekly_dir)

    weekly_dirs = sorted([child for child in WEEKLY_ROOT.iterdir() if child.is_dir()]) if WEEKLY_ROOT.exists() else []
    monthly_dir = MONTHLY_ROOT / month_bucket
    build_monthly_rollup(weekly_dirs, monthly_dir)

    if settings["annual_enabled"]:
        annual_dir = ANNUAL_ROOT / year_bucket
        build_annual_rollup(MONTHLY_ROOT, annual_dir)

    prune_by_age(WEEKLY_ROOT, keep_days=settings["weekly_days"])
    prune_by_age(MONTHLY_ROOT, keep_months=settings["monthly_months"])

    print(
        json.dumps(
            {
                "weekly_bucket": week_bucket,
                "monthly_bucket": month_bucket,
                "annual_bucket": year_bucket if settings["annual_enabled"] else None,
                "retention_root": str(RETENTION_ROOT),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
