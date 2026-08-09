from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import gspread
import pandas as pd
import yaml
from google.oauth2.service_account import Credentials
from gspread_dataframe import set_with_dataframe


SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


@dataclass(frozen=True)
class GoogleSheetsSettings:
    enabled: bool
    spreadsheet_id: str
    worksheet_prefix: str = "connectivity"
    service_account_json: str = ""
    service_account_file: str = ""
    clear_before_write: bool = True
    sync_provider_rows: bool = True
    sync_village_summary: bool = True
    sync_run_metadata: bool = True


def load_google_sheets_settings(config: dict[str, Any]) -> GoogleSheetsSettings:
    sheet_config = config.get("google_sheets", {})

    spreadsheet_id = (
        os.getenv("GOOGLE_SHEETS_SPREADSHEET_ID")
        or str(sheet_config.get("spreadsheet_id") or "")
    ).strip()
    worksheet_prefix = str(
        os.getenv("GOOGLE_SHEETS_WORKSHEET_PREFIX")
        or sheet_config.get("worksheet_prefix")
        or "connectivity"
    ).strip() or "connectivity"
    service_account_json = (
        os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
        or str(sheet_config.get("service_account_json") or "")
    ).strip()
    service_account_file = (
        os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE")
        or os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
        or str(sheet_config.get("service_account_file") or "")
    ).strip()

    enabled = bool(spreadsheet_id and (service_account_json or service_account_file))

    return GoogleSheetsSettings(
        enabled=enabled,
        spreadsheet_id=spreadsheet_id,
        worksheet_prefix=worksheet_prefix,
        service_account_json=service_account_json,
        service_account_file=service_account_file,
        clear_before_write=bool(sheet_config.get("clear_before_write", True)),
        sync_provider_rows=bool(sheet_config.get("sync_provider_rows", True)),
        sync_village_summary=bool(sheet_config.get("sync_village_summary", True)),
        sync_run_metadata=bool(sheet_config.get("sync_run_metadata", True)),
    )


def sync_pipeline_outputs(
    config: dict[str, Any],
    provider_rows: pd.DataFrame,
    source_recency: dict[str, str] | None = None,
) -> dict[str, Any]:
    settings = load_google_sheets_settings(config)
    if not settings.enabled:
        return {"enabled": False, "synced": False, "tabs": [], "run_id": None}

    client = build_client(settings)
    spreadsheet = client.open_by_key(settings.spreadsheet_id)
    run_id = f"{datetime.now(tz=UTC):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:10]}"
    synced_tabs: list[str] = []

    if settings.sync_provider_rows:
        provider_tab = f"{settings.worksheet_prefix}_provider_rows"
        provider_frame = provider_rows.copy()
        provider_frame["run_id"] = run_id
        provider_frame["synced_at_utc"] = datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")
        write_dataframe(spreadsheet, provider_tab, provider_frame, clear=settings.clear_before_write)
        synced_tabs.append(provider_tab)

    if settings.sync_village_summary:
        village_tab = f"{settings.worksheet_prefix}_village_summary"
        village_frame = build_village_summary_frame(provider_rows, run_id)
        village_frame["synced_at_utc"] = datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")
        if source_recency:
            for key, value in source_recency.items():
                village_frame[key] = value
        write_dataframe(spreadsheet, village_tab, village_frame, clear=settings.clear_before_write)
        synced_tabs.append(village_tab)

    if settings.sync_run_metadata:
        run_tab = f"{settings.worksheet_prefix}_runs"
        run_frame = build_run_metadata_frame(provider_rows, run_id, source_recency or {})
        write_dataframe(spreadsheet, run_tab, run_frame, clear=settings.clear_before_write)
        synced_tabs.append(run_tab)

    return {"enabled": True, "synced": True, "tabs": synced_tabs, "run_id": run_id}


def build_client(settings: GoogleSheetsSettings) -> gspread.Client:
    if settings.service_account_json:
        credentials_info = json.loads(settings.service_account_json)
        credentials = Credentials.from_service_account_info(credentials_info, scopes=SCOPES)
        return gspread.authorize(credentials)

    if settings.service_account_file:
        credentials = Credentials.from_service_account_file(settings.service_account_file, scopes=SCOPES)
        return gspread.authorize(credentials)

    raise ValueError("Google Sheets sync requires GOOGLE_SERVICE_ACCOUNT_JSON or GOOGLE_SERVICE_ACCOUNT_FILE.")


def write_dataframe(spreadsheet: gspread.Spreadsheet, tab_name: str, frame: pd.DataFrame, clear: bool = True) -> None:
    worksheet = get_or_create_worksheet(spreadsheet, tab_name, rows=max(len(frame) + 10, 100), cols=max(len(frame.columns) + 5, 10))
    if clear:
        worksheet.clear()
    if frame.empty:
        frame = pd.DataFrame(columns=list(frame.columns))
    set_with_dataframe(worksheet, frame, include_index=False, resize=True, allow_formulas=False)
    worksheet.freeze(1)


def get_or_create_worksheet(spreadsheet: gspread.Spreadsheet, tab_name: str, rows: int, cols: int) -> gspread.Worksheet:
    try:
        return spreadsheet.worksheet(tab_name)
    except gspread.WorksheetNotFound:
        return spreadsheet.add_worksheet(title=tab_name, rows=rows, cols=cols)


def build_village_summary_frame(provider_rows: pd.DataFrame, run_id: str) -> pd.DataFrame:
    metadata_columns = [
        column
        for column in [
            "opencellid_data_as_of",
            "opencellid_recency_basis",
            "ookla_data_as_of",
            "ookla_recency_basis",
            "data_as_of",
            "pipeline_generated_at_utc",
        ]
        if column in provider_rows.columns
    ]
    village_summary = (
        provider_rows.pivot_table(
            index=[
                "village_id",
                "state",
                "district",
                "block",
                "village",
                "lgd_code",
                "latitude",
                "longitude",
                "coordinate_source",
                *metadata_columns,
            ],
            columns="provider",
            values="coverage_score",
            aggfunc="first",
        )
        .reset_index()
    )
    renamed_columns: dict[str, str] = {}
    for column in village_summary.columns:
        if column in {
            "village_id",
            "state",
            "district",
            "block",
            "village",
            "lgd_code",
            "latitude",
            "longitude",
            "coordinate_source",
            *metadata_columns,
        }:
            continue
        renamed_columns[column] = f"{slugify(column)}_coverage_score"
    village_summary = village_summary.rename(columns=renamed_columns)
    village_summary.insert(0, "run_id", run_id)
    return village_summary


def build_run_metadata_frame(
    provider_rows: pd.DataFrame,
    run_id: str,
    source_recency: dict[str, str],
) -> pd.DataFrame:
    village_count = int(provider_rows["village_id"].nunique()) if "village_id" in provider_rows.columns else 0
    provider_count = int(provider_rows["provider"].nunique()) if "provider" in provider_rows.columns else 0
    summary = {
        "run_id": run_id,
        "generated_at_utc": datetime.now(tz=UTC).isoformat().replace("+00:00", "Z"),
        "village_count": village_count,
        "provider_count": provider_count,
        "provider_row_count": int(len(provider_rows)),
    }
    summary.update(source_recency)
    return pd.DataFrame([summary])


def slugify(value: str) -> str:
    return "".join(char.lower() if char.isalnum() else "_" for char in str(value)).strip("_")
