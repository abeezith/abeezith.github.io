"""Create one all-district provider table for Sheets and retention processing."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parent


def main() -> None:
    parser = argparse.ArgumentParser(description="Combine current requested-district provider estimates.")
    parser.add_argument("--root", type=Path, default=ROOT / "outputs" / "requested_districts")
    parser.add_argument("--output", type=Path, default=ROOT / "outputs" / "requested_district_provider_rows.csv.gz")
    args = parser.parse_args()

    files = sorted(args.root.glob("*/outputs/village_provider_signal_estimate.csv"))
    frames = [pd.read_csv(path, low_memory=False) for path in files]
    if not frames:
        raise FileNotFoundError("No requested-district provider CSV files were found.")
    combined = pd.concat(frames, ignore_index=True)
    combined["snapshot_captured_at_utc"] = datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    combined.to_csv(args.output, index=False, compression="gzip")
    print(f"Wrote {len(combined)} provider rows from {len(files)} districts to {args.output}")


if __name__ == "__main__":
    main()
