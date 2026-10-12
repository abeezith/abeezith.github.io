"""Normalize an LGD village export into the pipeline's canonical village schema."""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import pandas as pd


CANONICAL_COLUMNS = ["state", "district", "block", "village", "lgd_code"]
XML_NS = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def slugify(value: str) -> str:
    return "".join(char.lower() if char.isalnum() else "_" for char in value).strip("_")


def cell_value(cell: ET.Element) -> str:
    value = cell.find("x:v", XML_NS)
    if value is not None:
        return value.text or ""
    inline = cell.find("x:is", XML_NS)
    if inline is not None:
        return "".join(node.text or "" for node in inline.findall(".//x:t", XML_NS))
    return ""


def malformed_xlsx_rows(path: Path) -> pd.DataFrame:
    """Read cell values without loading the style XML that some LGD exports corrupt."""
    with ZipFile(path) as archive:
        sheet = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
    rows: list[list[str]] = []
    for row in sheet.findall("x:sheetData/x:row", XML_NS):
        values = [cell_value(cell) for cell in row.findall("x:c", XML_NS)]
        rows.append(values)
    if not rows:
        raise ValueError("The LGD workbook has no data rows.")
    headers = rows[0]
    return pd.DataFrame(rows[1:], columns=headers)


def read_lgd_export(path: Path) -> pd.DataFrame:
    if path.suffix.lower() in {".csv", ".txt"}:
        return pd.read_csv(path, dtype=str)
    try:
        return pd.read_excel(path, dtype=str)
    except Exception as exc:
        if path.suffix.lower() != ".xlsx":
            raise
        try:
            return malformed_xlsx_rows(path)
        except Exception:
            raise ValueError(f"Unable to read LGD export: {path}") from exc


def block_from_hierarchy(series: pd.Series) -> pd.Series:
    return series.fillna("").astype(str).str.extract(r"^\s*(.*?)\s*\(Sub-District\)", expand=False).fillna("").str.strip()


def normalize_lgd_export(frame: pd.DataFrame, state: str, district: str) -> pd.DataFrame:
    required = {"Village LGD Code", "Village Name (In English)", "Hierarchy"}
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"LGD export is missing required columns: {sorted(missing)}")
    villages = pd.DataFrame(
        {
            "state": state,
            "district": district,
            "block": block_from_hierarchy(frame["Hierarchy"]),
            "village": frame["Village Name (In English)"].fillna("").astype(str).str.strip(),
            "lgd_code": frame["Village LGD Code"].fillna("").astype(str).str.strip(),
        }
    )
    villages = villages[(villages["block"] != "") & (villages["village"] != "") & (villages["lgd_code"] != "")]
    return villages.drop_duplicates(CANONICAL_COLUMNS).sort_values(["block", "village", "lgd_code"]).reset_index(drop=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Import an LGD village export as a district-specific pipeline override.")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--state", required=True)
    parser.add_argument("--district", required=True)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    output = args.output or Path("outputs/lgd_district_overrides") / f"{slugify(args.state)}__{slugify(args.district)}.csv"
    villages = normalize_lgd_export(read_lgd_export(args.input), args.state, args.district)
    if villages.empty:
        raise ValueError("No usable village rows found in the LGD export.")
    output.parent.mkdir(parents=True, exist_ok=True)
    villages.to_csv(output, index=False)
    print(f"Wrote {len(villages):,} LGD villages across {villages['block'].nunique():,} blocks to {output}")


if __name__ == "__main__":
    main()
