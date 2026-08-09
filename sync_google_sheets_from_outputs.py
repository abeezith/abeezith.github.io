from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd
import yaml

from connectivity_pipeline.google_sheets_sync import sync_pipeline_outputs


def load_config(config_path: Path) -> dict:
    with config_path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync the latest pipeline outputs to Google Sheets.")
    parser.add_argument(
        "--config",
        default=Path(__file__).resolve().parent / "config.yaml",
        type=Path,
        help="Path to the YAML configuration file.",
    )
    parser.add_argument(
        "--provider-csv",
        default=Path(__file__).resolve().parent / "outputs" / "village_provider_signal_estimate.csv",
        type=Path,
        help="Path to the latest provider CSV output.",
    )
    args = parser.parse_args()

    if not args.provider_csv.exists():
        raise FileNotFoundError(f"Provider CSV not found: {args.provider_csv}")

    config = load_config(args.config)
    provider_rows = pd.read_csv(args.provider_csv)
    result = sync_pipeline_outputs(config=config, provider_rows=provider_rows)
    print(result)


if __name__ == "__main__":
    main()
