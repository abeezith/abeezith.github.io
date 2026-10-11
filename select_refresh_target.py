from __future__ import annotations

import argparse
import csv
import os
from datetime import date
from pathlib import Path


def load_districts(path: Path) -> list[tuple[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        rows = csv.DictReader(handle)
        districts = {
            (row["requested_state"].strip(), row["requested_district"].strip())
            for row in rows
            if row.get("requested_state") and row.get("requested_district")
        }
    if not districts:
        raise ValueError("No requested districts found in the supplied CSV.")
    return sorted(districts, key=lambda item: (item[0].casefold(), item[1].casefold()))


def main() -> None:
    parser = argparse.ArgumentParser(description="Select one requested district for a rolling daily refresh.")
    parser.add_argument("--requested-districts-csv", type=Path, required=True)
    parser.add_argument("--date", dest="run_date", default="", help="Optional ISO date for a reproducible selection.")
    parser.add_argument("--github-output", default="", help="Write GitHub Actions outputs to this file.")
    args = parser.parse_args()

    run_date = date.fromisoformat(args.run_date) if args.run_date else date.today()
    districts = load_districts(args.requested_districts_csv)
    state, district = districts[run_date.toordinal() % len(districts)]
    message = f"Selected {state} / {district} ({run_date.isoformat()}, {len(districts)}-district rotation)"
    print(message)

    output_path = args.github_output or os.getenv("GITHUB_OUTPUT", "")
    if output_path:
        with Path(output_path).open("a", encoding="utf-8") as handle:
            handle.write(f"state={state}\n")
            handle.write(f"district={district}\n")
            handle.write(f"run_date={run_date.isoformat()}\n")


if __name__ == "__main__":
    main()
