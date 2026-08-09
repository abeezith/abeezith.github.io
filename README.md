# Koraput Village Connectivity Pipeline

This project now supports district-by-district village connectivity estimation for the requested district set, while preserving the original Koraput block workflow.

For the current handoff and operating procedure, start here:

- [Setup and Runbook](./SETUP_AND_RUNBOOK.md)
- [Field Feedback Template](./FIELD_FEEDBACK_TEMPLATE.csv)
- [Project Progress Summary](./PROJECT_PROGRESS_SUMMARY.md)

The pipeline builds village-level mobile connectivity estimates from an Indian village master, OpenCellID tower data, and Ookla mobile tiles.

## Outputs

Running the pipeline writes:

- `village_provider_signal_estimate.csv`
- `village_connectivity_summary.xlsx`
- `village_connectivity.geojson`

All outputs are created inside the folder configured by `paths.output_dir` in [config.yaml](/E:/Resources/SecondBrain/koraput_connectivity_pipeline/config.yaml).

## Efficiency Notes

The project is now optimized for repeated district runs:

- OpenCellID district extracts are cached by buffered extent under `data/cache/opencellid/`, so rerunning the same district reuses the cached tower pull instead of calling the API again.
- The pipeline keeps the OpenCellID district extract in `data/opencellid_india.csv.gz`, so a normal rerun does not re-download towers unless you remove that file or change districts.
- The map generator now serializes only non-`Unknown` provider details into the HTML payload, which makes map rebuilds smaller and faster.
- The pipeline is constrained to current operators only: `Airtel`, `BSNL`, `Jio`, and `Vodafone Idea`.
- Requested-district status outputs now include an explicit confidence tier:
  - `high` for tower-enriched districts
  - `medium` for fallback districts with tower evidence
  - `low` for districts where OpenCellID was fetched but no usable tower-backed provider match was found
  - `none` for source-missing districts
- Legacy operator labels are normalized or suppressed before scoring so old names such as Aircel, MTNL, Tata Docomo, and similar variants do not leak into the current-operator outputs.

For the fastest local rerun after code or scoring changes, use:

```powershell
cd E:\Resources\SecondBrain\koraput_connectivity_pipeline
python run_pipeline.py
python generate_provider_map.py
```

That path reuses existing prepared inputs, the local tower extract, and any cached extent pull.

## Expected Inputs

Place the following files under `./data/` or update the paths in [config.yaml](/E:/Resources/SecondBrain/koraput_connectivity_pipeline/config.yaml):

- `villages.xlsx`: input workbook with columns `state, district, block, village, lgd_code, latitude, longitude`
- `lgd_village_master.xlsx`: LGD or village master data used to fill missing coordinates
- `opencellid_india.csv.gz`: OpenCellID India tower extract
- `ookla_mobile_tiles.geojson`: Ookla mobile open data tiles or another supported geospatial format

Supported formats:

- Village and LGD master input: `.xlsx`, `.xls`, `.csv`, `.parquet`
- OpenCellID input: `.csv`, `.csv.gz`, `.parquet`
- Ookla input: `.geojson`, `.gpkg`, `.shp`, `.parquet`, or tabular files with `geometry` WKT or `latitude`/`longitude`

## Install

```powershell
cd E:\Resources\SecondBrain\koraput_connectivity_pipeline
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Optional Google Sheets connection

The repo supports an opt-in Google Sheets sync. Keep secrets out of the repo and set them as environment variables instead:

Use [.env.example](./.env.example) as a template for local setup.

```powershell
$env:GOOGLE_SHEETS_SPREADSHEET_ID="YOUR_SPREADSHEET_ID"
$env:GOOGLE_SERVICE_ACCOUNT_JSON="{...full service account JSON...}"
```

If you prefer config-driven setup, you can also fill the `google_sheets` section in [config.yaml](/E:/Resources/SecondBrain/koraput_connectivity_pipeline/config.yaml) and set `enabled: true`.

The sync writes three tabs into one spreadsheet:

- `connectivity_provider_rows`
- `connectivity_village_summary`
- `connectivity_runs`

If you want GitHub to sync automatically, add these repository secrets:

- `GOOGLE_SHEETS_SPREADSHEET_ID`
- `GOOGLE_SERVICE_ACCOUNT_JSON`
- optional `GOOGLE_SHEETS_WORKSHEET_PREFIX`

Then the workflow at [.github/workflows/google-sheets-sync.yml](./.github/workflows/google-sheets-sync.yml) will sync the latest committed `outputs/village_provider_signal_estimate.csv` whenever it changes, or whenever you run the workflow manually.

## Run

```powershell
cd E:\Resources\SecondBrain\koraput_connectivity_pipeline
python .\prepare_koraput_inputs_from_kyl.py
python .\prepare_ookla_mobile_subset.py
python run_pipeline.py --config .\config.yaml
```

The helper script above uses your local [odisha_gp_village_to_subcentre_mapping_data.csv](/E:/Resources/SecondBrain/odisha_gp_village_to_subcentre_mapping_data.csv) plus the Odisha KYL service to prepare:

- `data/villages.xlsx`
- `data/lgd_village_master.xlsx`
- an empty `data/opencellid_india.csv.gz` placeholder
- `data/ookla_mobile_tiles.geojson` from the latest downloaded Ookla mobile parquet, if [prepare_ookla_mobile_subset.py](/E:/Resources/SecondBrain/koraput_connectivity_pipeline/prepare_ookla_mobile_subset.py) has been run

Replace the empty OpenCellID and Ookla placeholders with real extracts for a production run.

## Logic Summary

1. Filter the input file to `state=Odisha`, `district=Koraput`, `block=Koraput` unless config values are changed.
2. If village coordinates are missing, fill them from the LGD master using `lgd_code`, then fallback to a normalized `state+district+block+village` match.
3. Load OpenCellID tower data from local storage, or download it if the API section is enabled.
4. Spatially buffer each village centroid by the configured radius, then find all towers within that radius.
5. Map `MCC-MNC` combinations to Indian telecom providers using the config mapping.
6. Join Ookla mobile tiles to villages for location-level mobile performance context and optional provider-level context if the tile file includes provider names.
7. Score each provider for each village:
   - `Strong`: nearest provider tower within `strong_km`
   - `Moderate`: nearest provider tower within `moderate_km`, or strong Ookla evidence without a near tower
   - `Weak`: provider tower within `search_radius_km`, or weaker area-level Ookla evidence
   - `Unknown`: no tower or performance evidence
8. If Google Sheets is enabled, upsert the provider rows, village summary, and run metadata into the configured spreadsheet tabs.

## Recommended Workflow For India

To keep runs fast for all-India work done one district at a time:

1. Prepare one district input slice at a time.
2. Reuse the same Ookla subset only for the district being processed.
3. Keep `data/cache/opencellid/` between runs so repeated district work does not consume more OpenCellID requests.
4. Regenerate the HTML map only after the provider CSV changes.
5. Avoid deleting `data/opencellid_india.csv.gz` unless you intentionally want a fresh tower pull for a different district.

## Assumptions

- OpenCellID is treated as the primary provider-specific coverage signal.
- Ookla is supplemental. If the tile file is not provider-specific, it is used only as village-level context.
- Distances are computed in a projected CRS estimated from the village extent, which is appropriate for local 10 km search radii.
- The default provider mapping is a practical starter map, not a definitive historical registry for every Indian MNC assignment.
- The bundled `prepare_koraput_inputs_from_kyl.py` helper uses Odisha KYL village geometries as the working village-master source for Koraput block.

## Limitations

- OpenCellID is crowd-sourced and incomplete; missing towers do not prove absence of coverage.
- Tower presence does not guarantee signal quality, spectrum availability, or indoor performance.
- LGD and input village names may differ in spelling; the fallback name match is normalized but still exact on the normalized text.
- Ookla open data availability varies by tile and period. The current pipeline does not time-slice tiles by quarter unless the source file already does that.
- Provider-wise scoring is heuristic, not a licensed RF propagation model.
