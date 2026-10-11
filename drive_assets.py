"""Move large pipeline inputs between Google Drive and the CI workspace."""

from __future__ import annotations

import argparse
import io
import json
import os
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq


SCOPES = ["https://www.googleapis.com/auth/drive"]
SOURCE_FILES = (
    "stoptb_ben_screen_30_Jun_2026_mapped.csv",
    "phi_requested_districts.csv",
)


def drive_service():
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    raw = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    path = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip()
    if raw:
        info = json.loads(raw)
        credentials = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    elif path:
        credentials = service_account.Credentials.from_service_account_file(path, scopes=SCOPES)
    else:
        raise ValueError("Set GOOGLE_SERVICE_ACCOUNT_JSON or GOOGLE_SERVICE_ACCOUNT_FILE.")
    return build("drive", "v3", credentials=credentials, cache_discovery=False)


def list_folder_files(service, folder_id: str) -> list[dict]:
    query = f"'{folder_id}' in parents and trashed = false"
    files: list[dict] = []
    token = None
    while True:
        response = service.files().list(
            q=query,
            fields="nextPageToken,files(id,name,modifiedTime,size)",
            orderBy="name",
            pageToken=token,
        ).execute()
        files.extend(response.get("files", []))
        token = response.get("nextPageToken")
        if not token:
            return files


def download_file(service, file_id: str, destination: Path) -> None:
    from googleapiclient.http import MediaIoBaseDownload

    destination.parent.mkdir(parents=True, exist_ok=True)
    request = service.files().get_media(fileId=file_id)
    with destination.open("wb") as handle:
        downloader = MediaIoBaseDownload(handle, request)
        complete = False
        while not complete:
            _, complete = downloader.next_chunk()


def download_inputs(folder_id: str, destination: Path) -> None:
    service = drive_service()
    files = list_folder_files(service, folder_id)
    by_name = {item["name"]: item for item in files}
    missing = [name for name in SOURCE_FILES if name not in by_name]
    if missing:
        raise FileNotFoundError(f"Drive input files not found: {', '.join(missing)}")

    for name in SOURCE_FILES:
        download_file(service, by_name[name]["id"], destination / name)

    ookla_parts = [item for item in files if item["name"].startswith("ookla_mobile_tiles_q1_2026.part-") and item["name"].endswith(".parquet")]
    if not ookla_parts:
        raise FileNotFoundError("No split Ookla Parquet parts were found in the Drive folder.")
    for item in ookla_parts:
        download_file(service, item["id"], destination / "ookla_mobile_tiles_q1_2026" / item["name"])

    print(json.dumps({"downloaded_files": len(SOURCE_FILES) + len(ookla_parts), "destination": str(destination)}, indent=2))


def retention_name(relative_path: Path) -> str:
    return "retention__" + "__".join(relative_path.parts)


def retention_path(name: str) -> Path:
    return Path(*name.removeprefix("retention__").split("__"))


def download_retention(folder_id: str, destination: Path) -> None:
    service = drive_service()
    files = [item for item in list_folder_files(service, folder_id) if item["name"].startswith("retention__")]
    for item in files:
        download_file(service, item["id"], destination / retention_path(item["name"]))
    print(json.dumps({"downloaded_files": len(files), "destination": str(destination)}, indent=2))


def upload_retention(folder_id: str, source: Path) -> None:
    from googleapiclient.http import MediaFileUpload

    service = drive_service()
    existing = {item["name"]: item for item in list_folder_files(service, folder_id) if item["name"].startswith("retention__")}
    expected: set[str] = set()
    for path in sorted(source.rglob("*")):
        if not path.is_file():
            continue
        name = retention_name(path.relative_to(source))
        expected.add(name)
        media = MediaFileUpload(str(path), resumable=True)
        if name in existing:
            service.files().update(fileId=existing[name]["id"], media_body=media).execute()
        else:
            service.files().create(body={"name": name, "parents": [folder_id]}, media_body=media, fields="id").execute()
    for name, item in existing.items():
        if name not in expected:
            service.files().update(fileId=item["id"], body={"trashed": True}).execute()
    print(json.dumps({"uploaded_files": len(expected), "removed_expired_files": len(set(existing) - expected)}, indent=2))


def split_parquet(source: Path, destination: Path, target_megabytes: int) -> None:
    if target_megabytes < 16:
        raise ValueError("target_megabytes must be at least 16.")
    destination.mkdir(parents=True, exist_ok=True)
    parquet = pq.ParquetFile(source)
    source_size = source.stat().st_size
    rows_per_part = max(1, int(parquet.metadata.num_rows * (target_megabytes * 1024 * 1024) / source_size))
    part = 0
    buffer: list[pa.Table] = []
    buffered_rows = 0

    def write_part(tables: list[pa.Table], index: int) -> None:
        table = pa.concat_tables(tables)
        output = destination / f"ookla_mobile_tiles_q1_2026.part-{index:03d}.parquet"
        pq.write_table(table, output, compression="snappy")
        if output.stat().st_size > 95 * 1024 * 1024:
            raise ValueError(f"{output.name} exceeds the 95 MB upload safety limit; rerun with a lower target.")

    for batch in parquet.iter_batches(batch_size=min(rows_per_part, 250_000)):
        table = pa.Table.from_batches([batch])
        buffer.append(table)
        buffered_rows += table.num_rows
        if buffered_rows >= rows_per_part:
            write_part(buffer, part)
            part += 1
            buffer = []
            buffered_rows = 0
    if buffer:
        write_part(buffer, part)
    print(json.dumps({"parts": part + bool(buffer), "destination": str(destination)}, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare or download Drive-backed pipeline inputs.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    download = subparsers.add_parser("download-inputs")
    download.add_argument("--folder-id", required=True)
    download.add_argument("--destination", type=Path, required=True)
    download_retained = subparsers.add_parser("download-retention")
    download_retained.add_argument("--folder-id", required=True)
    download_retained.add_argument("--destination", type=Path, required=True)
    upload_retained = subparsers.add_parser("upload-retention")
    upload_retained.add_argument("--folder-id", required=True)
    upload_retained.add_argument("--source", type=Path, required=True)
    split = subparsers.add_parser("split-ookla")
    split.add_argument("--source", type=Path, required=True)
    split.add_argument("--destination", type=Path, required=True)
    split.add_argument("--target-megabytes", type=int, default=40)
    args = parser.parse_args()
    if args.command == "download-inputs":
        download_inputs(args.folder_id, args.destination)
    elif args.command == "download-retention":
        download_retention(args.folder_id, args.destination)
    elif args.command == "upload-retention":
        upload_retention(args.folder_id, args.source)
    else:
        split_parquet(args.source, args.destination, args.target_megabytes)


if __name__ == "__main__":
    main()
