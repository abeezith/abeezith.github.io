from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parent
STATUS_CSV = ROOT / "outputs" / "requested_districts" / "requested_district_status.csv"
ENRICH_SCRIPT = ROOT / "enrich_requested_districts_from_stoptb.py"
PYTHON_EXE = ROOT / ".venv" / "Scripts" / "python.exe"


def main() -> None:
    parser = argparse.ArgumentParser(description="Run pending requested districts one by one.")
    parser.add_argument("--limit", type=int, default=0, help="Optional limit on number of pending districts to run.")
    args = parser.parse_args()

    status_df = pd.read_csv(STATUS_CSV)
    pending = status_df[status_df["status"] == "pending_fetch"].copy()
    if args.limit > 0:
        pending = pending.head(args.limit)

    if pending.empty:
        print("No pending requested districts found.")
        return

    for row in pending.itertuples(index=False):
        print(f"Running {row.requested_state} | {row.requested_district}")
        subprocess.run(
            [
                str(PYTHON_EXE),
                str(ENRICH_SCRIPT),
                "--requested-state",
                str(row.requested_state),
                "--requested-district",
                str(row.requested_district),
            ],
            check=True,
            cwd=str(ROOT),
        )


if __name__ == "__main__":
    main()
