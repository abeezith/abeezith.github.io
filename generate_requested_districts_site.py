from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from archive_utils import archive_existing_path
from generate_provider_map import (
    PROVIDER_STYLES,
    SCORE_COLORS,
    SCORE_ORDER,
    build_feature_rows,
)


ROOT = Path(__file__).resolve().parent
SUMMARY_CSV = ROOT / "outputs" / "requested_districts" / "requested_district_summary.csv"
STATUS_CSV = ROOT / "outputs" / "requested_districts" / "requested_district_status.csv"
SITE_ROOT = ROOT / "outputs" / "requested_districts_site"
OUTPUT_HTML = SITE_ROOT / "index.html"
SUMMARY_JSON = ROOT / "outputs" / "requested_districts" / "requested_district_summary.json"


def load_combined_features(summary_df: pd.DataFrame) -> list[dict[str, object]]:
    all_features: list[dict[str, object]] = []
    for row in summary_df.itertuples(index=False):
        provider_csv = (SITE_ROOT / getattr(row, "relative_provider_csv")).resolve()
        provider_df = pd.read_csv(provider_csv)
        _, features = build_feature_rows(provider_df)
        for feature in features:
            feature["properties"]["summary_row_count"] = int(getattr(row, "row_count"))
            feature["properties"]["summary_gps_village_count"] = int(getattr(row, "gps_village_count"))
        all_features.extend(features)
    return all_features


def fmt_timestamp(path: Path) -> str:
    if not path.exists():
        return ""
    return path.stat().st_mtime_ns and pd.Timestamp(path.stat().st_mtime, unit="s").strftime("%Y-%m-%d %H:%M")


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


def build_status_records(summary_df: pd.DataFrame, status_df: pd.DataFrame) -> list[dict[str, object]]:
    summary_lookup = {
        (str(row.state), str(row.district)): row
        for row in summary_df.itertuples(index=False)
    }
    records: list[dict[str, object]] = []
    for row in status_df.itertuples(index=False):
        key = (str(row.requested_state), str(row.requested_district))
        summary_row = summary_lookup.get(key)
        output_dir = Path(str(row.output_dir)) if str(getattr(row, "output_dir", "")).strip() else None
        provider_csv = output_dir / "outputs" / "village_provider_signal_estimate.csv" if output_dir else None
        tower_file = output_dir / "data" / "opencellid_district.csv.gz" if output_dir else None

        village_count = int(getattr(summary_row, "village_count", 0)) if summary_row is not None else 0
        gps_village_count = int(getattr(summary_row, "gps_village_count", 0)) if summary_row is not None else 0
        gps_pct = round((gps_village_count / village_count) * 100, 1) if village_count else 0.0

        tower_rows = 0
        non_unknown_rows = 0
        if provider_csv and provider_csv.exists():
            provider_df = pd.read_csv(provider_csv, usecols=["coverage_score", "nearest_tower_km"])
            tower_rows = int(provider_df["nearest_tower_km"].notna().sum())
            non_unknown_rows = int((provider_df["coverage_score"] != "Unknown").sum())

        status = str(row.status)
        if status == "completed_tower_enriched":
            data_kind = "OpenCellID + Ookla; tower-backed provider matches"
        elif status == "fallback_tower_enriched":
            data_kind = "Fallback district proxy geocoding + OpenCellID + Ookla"
        elif status == "fetched_no_tower_evidence":
            data_kind = "OpenCellID fetched + Ookla; no usable tower-backed provider matches"
        elif status == "fallback_no_tower_evidence":
            data_kind = "Fallback district proxy geocoding; OpenCellID fetched but no usable tower-backed provider matches"
        elif status == "source_missing":
            data_kind = "No village-level source rows available"
        else:
            data_kind = "Pending enrichment"

        records.append(
            {
                "requested_state": str(row.requested_state),
                "requested_district": str(row.requested_district),
                "matched_state": str(row.matched_state),
                "matched_district": str(row.matched_district),
                "status": status,
                "confidence_level": confidence_level_for_status(status),
                "data_kind": data_kind,
                "source_row_count": int(row.source_row_count),
                "source_village_count": int(row.source_village_count),
                "mapped_village_count": village_count,
                "gps_village_count": gps_village_count,
                "gps_coverage_pct": gps_pct,
                "tower_file_exists": bool(row.has_opencellid_file),
                "tower_file_size_bytes": int(row.opencellid_file_size_bytes),
                "tower_rows": tower_rows,
                "non_unknown_rows": non_unknown_rows,
                "provider_csv_updated": fmt_timestamp(provider_csv) if provider_csv else "",
                "tower_file_updated": fmt_timestamp(tower_file) if tower_file else "",
            }
        )
    return records


def build_html(
    summary_df: pd.DataFrame,
    status_df: pd.DataFrame,
    missing_districts: list[dict[str, str]],
    features: list[dict[str, object]],
) -> str:
    states = sorted(summary_df["state"].dropna().astype(str).unique().tolist())
    district_map = {
        state: sorted(
            summary_df.loc[summary_df["state"] == state, "district"].dropna().astype(str).unique().tolist()
        )
        for state in states
    }
    status_map = {
        f"{row.requested_state}|||{row.requested_district}": str(row.status)
        for row in status_df.itertuples(index=False)
    }
    status_counts = {str(key): int(value) for key, value in status_df["status"].value_counts().to_dict().items()}
    status_records = build_status_records(summary_df, status_df)
    total_villages = int(summary_df["village_count"].sum())
    total_gps_villages = int(summary_df["gps_village_count"].sum())

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Requested District Connectivity</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
  <style>
    body {{
      margin: 0;
      font-family: Georgia, "Times New Roman", serif;
      background: linear-gradient(180deg, #f7f3e8 0%, #eef1e5 100%);
      color: #223127;
    }}
    #app {{
      display: grid;
      grid-template-columns: 390px 1fr;
      min-height: 100vh;
    }}
    #sidebar {{
      padding: 20px 18px 28px;
      border-right: 1px solid #d7ddc8;
      background: rgba(255, 252, 246, 0.96);
      overflow-y: auto;
    }}
    #map {{
      height: 100vh;
      width: 100%;
    }}
    #main {{
      min-height: 100vh;
    }}
    #statusView {{
      display: none;
      height: 100vh;
      overflow: auto;
      padding: 22px 24px 30px;
      box-sizing: border-box;
    }}
    h1 {{
      margin: 0 0 8px;
      font-size: 28px;
      line-height: 1.1;
    }}
    .muted {{
      color: #4f5c4f;
      font-size: 14px;
      margin-bottom: 16px;
    }}
    .control {{
      margin-bottom: 16px;
    }}
    .control label {{
      display: block;
      margin-bottom: 6px;
      font-weight: 700;
      font-size: 13px;
      letter-spacing: 0.04em;
      text-transform: uppercase;
    }}
    select, input[type="text"] {{
      width: 100%;
      padding: 10px 12px;
      border-radius: 10px;
      border: 1px solid #b7c0a4;
      background: #fffef9;
      font-size: 14px;
      box-sizing: border-box;
    }}
    .checks {{
      display: grid;
      gap: 8px;
    }}
    .checks label {{
      display: flex;
      align-items: center;
      gap: 10px;
      font-size: 14px;
      font-weight: 400;
      text-transform: none;
      letter-spacing: 0;
    }}
    .swatch {{
      width: 14px;
      height: 14px;
      border-radius: 50%;
      display: inline-block;
      border: 1px solid rgba(0,0,0,0.12);
    }}
    .panel {{
      margin-top: 14px;
      padding: 12px;
      border-radius: 12px;
      background: #f5f1e5;
      border: 1px solid #ddd5bf;
      font-size: 13px;
    }}
    .stat {{
      margin-top: 16px;
      padding: 14px;
      border-radius: 12px;
      background: #223127;
      color: #f6f4eb;
    }}
    .stat strong {{
      display: block;
      font-size: 28px;
      margin-bottom: 4px;
    }}
    .mini {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
      margin-top: 14px;
    }}
    .mini .box {{
      padding: 12px;
      border-radius: 12px;
      background: #fffdf8;
      border: 1px solid #ddd5bf;
    }}
    .mini .box strong {{
      display: block;
      font-size: 20px;
      margin-bottom: 2px;
    }}
    .status-grid {{
      display: grid;
      gap: 8px;
      margin-top: 10px;
    }}
    .status-chip {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      padding: 8px 10px;
      border-radius: 10px;
      background: #fffdf8;
      border: 1px solid #ddd5bf;
      font-size: 12px;
    }}
    .status-chip strong {{
      font-size: 12px;
    }}
    .status-dot {{
      width: 10px;
      height: 10px;
      border-radius: 50%;
      display: inline-block;
      margin-right: 8px;
      flex: 0 0 auto;
    }}
    .district-status-list {{
      display: grid;
      gap: 8px;
      margin-top: 8px;
    }}
    .district-status-item {{
      padding: 8px 10px;
      border-radius: 10px;
      background: #fffdf8;
      border: 1px solid #ddd5bf;
      font-size: 12px;
    }}
    .district-status-item strong {{
      display: block;
      margin-bottom: 3px;
    }}
    .status-tag {{
      display: inline-block;
      padding: 2px 7px;
      border-radius: 999px;
      font-size: 11px;
      font-weight: 700;
      margin-top: 4px;
      background: #e8eadf;
      color: #223127;
    }}
    .tabs {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 8px;
      margin-bottom: 16px;
    }}
    .tab-btn {{
      border: 1px solid #c9cfbb;
      background: #fffdf8;
      color: #223127;
      border-radius: 10px;
      padding: 10px 12px;
      font-family: inherit;
      font-size: 13px;
      font-weight: 700;
      cursor: pointer;
    }}
    .tab-btn.active {{
      background: #223127;
      color: #f6f4eb;
      border-color: #223127;
    }}
    .status-hero {{
      display: grid;
      gap: 14px;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      margin-bottom: 18px;
    }}
    .status-card {{
      padding: 14px;
      border-radius: 14px;
      background: #fffdf8;
      border: 1px solid #ddd5bf;
    }}
    .status-card strong {{
      display: block;
      font-size: 22px;
      margin-bottom: 2px;
    }}
    .status-table-wrap {{
      background: #fffdf8;
      border: 1px solid #ddd5bf;
      border-radius: 16px;
      overflow: hidden;
    }}
    .status-table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 12px;
    }}
    .status-table th,
    .status-table td {{
      padding: 9px 10px;
      border-bottom: 1px solid #ece7d8;
      vertical-align: top;
      text-align: left;
    }}
    .status-table th {{
      background: #f5f1e5;
      position: sticky;
      top: 0;
      z-index: 1;
    }}
    .quality-note {{
      font-size: 12px;
      color: #556255;
      margin-bottom: 14px;
    }}
    .tiny-tag {{
      display: inline-block;
      padding: 2px 6px;
      border-radius: 999px;
      font-size: 10px;
      font-weight: 700;
      background: #edf1e2;
      border: 1px solid #d5dcc4;
      color: #223127;
      margin-top: 4px;
    }}
    .confidence-tag {{
      display: inline-block;
      padding: 2px 6px;
      border-radius: 999px;
      font-size: 10px;
      font-weight: 700;
      margin-top: 4px;
    }}
    .provider-box {{
      width: 30px;
      min-height: 30px;
      display: grid;
      grid-template-columns: repeat(2, 1fr);
      grid-template-rows: repeat(2, 1fr);
      align-items: center;
      justify-items: center;
      gap: 1px 2px;
      padding: 3px;
      box-sizing: border-box;
      border: 1.5px solid rgba(36, 49, 31, 0.9);
      border-radius: 7px;
      background: rgba(255, 254, 249, 0.96);
      color: #24311f;
      font-weight: 700;
      line-height: 1;
      box-shadow: 0 1px 4px rgba(0, 0, 0, 0.2);
    }}
    .provider-box-proxy {{
      border-style: dashed;
      background: rgba(245, 241, 229, 0.98);
      box-shadow: 0 1px 4px rgba(0, 0, 0, 0.12);
    }}
    .provider-letter {{
      display: inline-block;
      font-size: 9px;
      font-weight: 700;
    }}
    .popup-table {{
      border-collapse: collapse;
      width: 100%;
      margin-top: 8px;
      font-size: 12px;
    }}
    .popup-table td {{
      padding: 4px 6px;
      border-bottom: 1px solid #e6e6e6;
      vertical-align: top;
    }}
    .leaflet-tooltip.provider-label {{
      background: rgba(255,255,255,0.94);
      border: 1px solid #d5d5d5;
      color: #24311f;
      box-shadow: none;
      padding: 4px 6px;
      border-radius: 6px;
      font-size: 11px;
    }}
    .leaflet-tooltip.provider-label:before {{
      display: none;
    }}
    ul {{
      margin: 8px 0 0 16px;
      padding: 0;
    }}
  </style>
</head>
<body>
  <div id="app">
    <aside id="sidebar">
      <h1>Requested District Connectivity</h1>
      <div class="muted">One combined village map for all generated districts. Use the state and district selectors to narrow the view without leaving the page.</div>

      <div class="tabs">
        <button id="mapTab" class="tab-btn active" type="button">Map</button>
        <button id="statusTab" class="tab-btn" type="button">Status</button>
      </div>

      <div class="control">
        <label for="stateFilter">State</label>
        <select id="stateFilter">
          <option value="">All states</option>
        </select>
      </div>

      <div class="control">
        <label for="districtFilter">District</label>
        <select id="districtFilter">
          <option value="">All districts</option>
        </select>
      </div>

      <div class="control">
        <label for="villageSearch">Village Search</label>
        <input id="villageSearch" type="text" placeholder="Type a village name" />
      </div>

      <div class="control">
        <label>Visible Scores</label>
        <div class="checks" id="scoreFilters"></div>
      </div>

      <div class="panel">
        <div><strong>Color Guide</strong></div>
        <div>Strong = dark green</div>
        <div>Moderate = sand yellow</div>
        <div>Weak = orange-red</div>
        <div>Unknown = grey</div>
      </div>

      <div class="panel">
        <div><strong>Provider Letters</strong></div>
        <div>A = Airtel</div>
        <div>B = BSNL</div>
        <div>J = Jio</div>
        <div>V = Vodafone Idea</div>
      </div>

      <div class="stat">
        <strong id="visibleCount">0</strong>
        villages visible on map
      </div>

      <div class="mini">
        <div class="box"><strong>{total_villages}</strong> total villages</div>
        <div class="box"><strong>{total_gps_villages}</strong> mapped villages</div>
        <div class="box"><strong>{len(states)}</strong> states</div>
        <div class="box"><strong>{len(summary_df)}</strong> districts</div>
      </div>

      <div class="panel">
        <div><strong>District Status</strong></div>
        <div class="status-grid">
          <div class="status-chip"><span><span class="status-dot" style="background:#2f855a"></span><strong>Tower Enriched</strong></span><span>{status_counts.get("completed_tower_enriched", 0)}</span></div>
          <div class="status-chip"><span><span class="status-dot" style="background:#276749"></span><strong>Fallback Tower Enriched</strong></span><span>{status_counts.get("fallback_tower_enriched", 0)}</span></div>
          <div class="status-chip"><span><span class="status-dot" style="background:#d69e2e"></span><strong>Fetched, No Tower Evidence</strong></span><span>{status_counts.get("fetched_no_tower_evidence", 0)}</span></div>
          <div class="status-chip"><span><span class="status-dot" style="background:#b7791f"></span><strong>Fallback, No Tower Evidence</strong></span><span>{status_counts.get("fallback_no_tower_evidence", 0)}</span></div>
          <div class="status-chip"><span><span class="status-dot" style="background:#718096"></span><strong>Source Missing</strong></span><span>{status_counts.get("source_missing", 0)}</span></div>
        </div>
      </div>

      <div class="panel">
        <div><strong>Visible District Status</strong></div>
        <div class="district-status-list" id="districtStatusList"></div>
      </div>

      <div class="panel">
        <div><strong>Missing Requested Districts</strong></div>
        <ul id="missingList"></ul>
      </div>
    </aside>
    <div id="main">
      <div id="map"></div>
      <section id="statusView">
        <div class="status-hero">
          <div class="status-card"><strong>{len(status_records)}</strong> requested districts</div>
          <div class="status-card"><strong>{status_counts.get("completed_tower_enriched", 0)}</strong> tower enriched</div>
          <div class="status-card"><strong>{status_counts.get("fallback_tower_enriched", 0)}</strong> fallback tower enriched</div>
          <div class="status-card"><strong>{status_counts.get("fetched_no_tower_evidence", 0)}</strong> fetched, no tower evidence</div>
          <div class="status-card"><strong>{status_counts.get("fallback_no_tower_evidence", 0)}</strong> fallback, no tower evidence</div>
          <div class="status-card"><strong>{status_counts.get("source_missing", 0)}</strong> source missing</div>
        </div>
        <div class="quality-note">Data quality indicators below reflect current local outputs: village-level source row availability, mapped village count, GPS coverage among mapped villages, whether an OpenCellID district file exists, whether tower-backed provider rows were actually used, and the last modified timestamps of the provider CSV and district tower file.</div>
        <div class="status-table-wrap">
          <table class="status-table">
            <thead>
              <tr>
                <th>Requested District</th>
                <th>Status</th>
                <th>Confidence</th>
                <th>Data Type</th>
                <th>Source Rows</th>
                <th>Mapped Villages</th>
                <th>GPS Quality</th>
                <th>Tower Evidence</th>
                <th>Recency</th>
              </tr>
            </thead>
            <tbody id="statusTableBody"></tbody>
          </table>
        </div>
      </section>
    </div>
  </div>

  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <script>
    const scoreOrder = {json.dumps(SCORE_ORDER)};
    const scoreColors = {json.dumps(SCORE_COLORS)};
    const providerStyles = {json.dumps(PROVIDER_STYLES)};
    const stateOptions = {json.dumps(states)};
    const districtOptions = {json.dumps(district_map)};
    const districtStatusMap = {json.dumps(status_map)};
    const statusRecords = {json.dumps(status_records)};
    const missingDistricts = {json.dumps(missing_districts)};
    const geojson = {json.dumps({"type": "FeatureCollection", "features": features})};

    const mapTab = document.getElementById("mapTab");
    const statusTab = document.getElementById("statusTab");
    const mapView = document.getElementById("map");
    const statusView = document.getElementById("statusView");
    const stateFilter = document.getElementById("stateFilter");
    const districtFilter = document.getElementById("districtFilter");
    const villageSearch = document.getElementById("villageSearch");
    const scoreFilters = document.getElementById("scoreFilters");
    const visibleCount = document.getElementById("visibleCount");
    const missingList = document.getElementById("missingList");
    const districtStatusList = document.getElementById("districtStatusList");
    const statusTableBody = document.getElementById("statusTableBody");

    let selectedState = "";
    let selectedDistrict = "";
    let selectedScores = new Set(scoreOrder);
    let searchText = "";

    for (const state of stateOptions) {{
      const option = document.createElement("option");
      option.value = state;
      option.textContent = state;
      stateFilter.appendChild(option);
    }}

    function statusLabel(status) {{
      if (status === "completed_tower_enriched") return "Tower enriched";
      if (status === "fallback_tower_enriched") return "Fallback tower enriched";
      if (status === "fetched_no_tower_evidence") return "Fetched, no tower evidence";
      if (status === "fallback_no_tower_evidence") return "Fallback, no tower evidence";
      if (status === "source_missing") return "Source missing";
      return "Pending";
    }}

    function statusColor(status) {{
      if (status === "completed_tower_enriched") return "#2f855a";
      if (status === "fallback_tower_enriched") return "#276749";
      if (status === "fetched_no_tower_evidence") return "#d69e2e";
      if (status === "fallback_no_tower_evidence") return "#b7791f";
      if (status === "source_missing") return "#718096";
      return "#4a5568";
    }}

    function confidenceLabel(confidence) {{
      if (confidence === "high") return "High";
      if (confidence === "medium") return "Medium";
      if (confidence === "low") return "Low";
      if (confidence === "none") return "None";
      return "Pending";
    }}

    function confidenceColor(confidence) {{
      if (confidence === "high") return "#2f855a";
      if (confidence === "medium") return "#276749";
      if (confidence === "low") return "#b7791f";
      if (confidence === "none") return "#718096";
      return "#4a5568";
    }}

    function fillDistrictOptions() {{
      districtFilter.innerHTML = '<option value="">All districts</option>';
      const districts = selectedState ? (districtOptions[selectedState] || []) : Object.values(districtOptions).flat();
      const seen = new Set();
      for (const district of districts) {{
        if (seen.has(district)) {{
          continue;
        }}
        seen.add(district);
        const option = document.createElement("option");
        option.value = district;
        const stateForDistrict = selectedState || Object.keys(districtOptions).find((state) => (districtOptions[state] || []).includes(district)) || "";
        const status = districtStatusMap[`${{stateForDistrict}}|||${{district}}`];
        option.textContent = status ? `${{district}} [${{statusLabel(status)}}]` : district;
        districtFilter.appendChild(option);
      }}
      if (selectedDistrict && !seen.has(selectedDistrict)) {{
        selectedDistrict = "";
      }}
      districtFilter.value = selectedDistrict;
    }}

    function refreshDistrictStatusList() {{
      districtStatusList.innerHTML = "";
      const pairs = [];
      for (const state of stateOptions) {{
        if (selectedState && state !== selectedState) {{
          continue;
        }}
        for (const district of (districtOptions[state] || [])) {{
          if (selectedDistrict && district !== selectedDistrict) {{
            continue;
          }}
          pairs.push({{ state, district }});
        }}
      }}
      const limited = pairs.slice(0, 12);
      for (const pair of limited) {{
        const status = districtStatusMap[`${{pair.state}}|||${{pair.district}}`] || "pending_fetch";
        const item = document.createElement("div");
        item.className = "district-status-item";
        item.innerHTML = `<strong>${{pair.state}} | ${{pair.district}}</strong><span class="status-tag" style="background:${{statusColor(status)}}22;color:${{statusColor(status)}};border:1px solid ${{statusColor(status)}}55">${{statusLabel(status)}}</span>`;
        districtStatusList.appendChild(item);
      }}
      if (pairs.length > limited.length) {{
        const more = document.createElement("div");
        more.className = "district-status-item";
        more.textContent = `+${{pairs.length - limited.length}} more districts in current filter`;
        districtStatusList.appendChild(more);
      }}
      if (!pairs.length) {{
        const none = document.createElement("div");
        none.className = "district-status-item";
        none.textContent = "No districts match the current filter.";
        districtStatusList.appendChild(none);
      }}
    }}

    function filteredStatusRecords() {{
      return statusRecords.filter((row) => {{
        const matchesState = !selectedState || row.requested_state === selectedState;
        const matchesDistrict = !selectedDistrict || row.requested_district === selectedDistrict;
        const matchesSearch = !searchText || row.requested_district.toLowerCase().includes(searchText) || row.matched_district.toLowerCase().includes(searchText);
        return matchesState && matchesDistrict && matchesSearch;
      }});
    }}

    function refreshStatusTable() {{
      statusTableBody.innerHTML = "";
      for (const row of filteredStatusRecords()) {{
        const tr = document.createElement("tr");
        const towerText = row.tower_file_exists
          ? `${{row.tower_rows}} provider rows with tower distance`
          : "No OpenCellID district file";
        const recencyText = row.provider_csv_updated || row.tower_file_updated
          ? `Provider CSV: ${{row.provider_csv_updated || "NA"}}<br/>Tower file: ${{row.tower_file_updated || "NA"}}`
          : "Not available";
        tr.innerHTML = `
          <td><strong>${{row.requested_state}} | ${{row.requested_district}}</strong><div class="tiny-tag">Matched: ${{row.matched_district}}</div></td>
          <td><span class="status-tag" style="background:${{statusColor(row.status)}}22;color:${{statusColor(row.status)}};border:1px solid ${{statusColor(row.status)}}55">${{statusLabel(row.status)}}</span></td>
          <td><span class="confidence-tag" style="background:${{confidenceColor(row.confidence_level)}}22;color:${{confidenceColor(row.confidence_level)}};border:1px solid ${{confidenceColor(row.confidence_level)}}55">${{confidenceLabel(row.confidence_level)}}</span></td>
          <td>${{row.data_kind}}</td>
          <td>${{row.source_row_count}} rows<br/>${{row.source_village_count}} source villages</td>
          <td>${{row.mapped_village_count}} mapped villages</td>
          <td>${{row.gps_village_count}} with GPS<br/>${{row.gps_coverage_pct}}% coverage</td>
          <td>${{towerText}}<br/>OpenCellID file: ${{row.tower_file_exists ? "Yes" : "No"}}${{row.tower_file_exists ? ` (${{row.tower_file_size_bytes}} bytes)` : ""}}</td>
          <td>${{recencyText}}</td>
        `;
        statusTableBody.appendChild(tr);
      }}
      if (!statusTableBody.children.length) {{
        const tr = document.createElement("tr");
        tr.innerHTML = `<td colspan="9">No districts match the current filters.</td>`;
        statusTableBody.appendChild(tr);
      }}
    }}

    function showMapView() {{
      mapView.style.display = "block";
      statusView.style.display = "none";
      mapTab.classList.add("active");
      statusTab.classList.remove("active");
      setTimeout(() => map.invalidateSize(), 50);
    }}

    function showStatusView() {{
      mapView.style.display = "none";
      statusView.style.display = "block";
      mapTab.classList.remove("active");
      statusTab.classList.add("active");
      refreshStatusTable();
    }}

    for (const item of missingDistricts) {{
      const li = document.createElement("li");
      li.textContent = `${{item.state}} | ${{item.district}}`;
      missingList.appendChild(li);
    }}
    if (!missingDistricts.length) {{
      const li = document.createElement("li");
      li.textContent = "None";
      missingList.appendChild(li);
    }}

    for (const score of scoreOrder) {{
      const wrapper = document.createElement("label");
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.checked = true;
      checkbox.value = score;
      checkbox.addEventListener("change", () => {{
        if (checkbox.checked) {{
          selectedScores.add(score);
        }} else {{
          selectedScores.delete(score);
        }}
        refreshMarkers();
      }});
      const swatch = document.createElement("span");
      swatch.className = "swatch";
      swatch.style.background = scoreColors[score];
      const text = document.createElement("span");
      text.textContent = score;
      wrapper.appendChild(checkbox);
      wrapper.appendChild(swatch);
      wrapper.appendChild(text);
      scoreFilters.appendChild(wrapper);
    }}

    stateFilter.addEventListener("change", () => {{
      selectedState = stateFilter.value;
      selectedDistrict = "";
      fillDistrictOptions();
      refreshDistrictStatusList();
      refreshMarkers(true);
      refreshStatusTable();
    }});

    districtFilter.addEventListener("change", () => {{
      selectedDistrict = districtFilter.value;
      refreshDistrictStatusList();
      refreshMarkers(true);
      refreshStatusTable();
    }});

    villageSearch.addEventListener("input", () => {{
      searchText = villageSearch.value.trim().toLowerCase();
      refreshMarkers(true);
      refreshStatusTable();
    }});

    mapTab.addEventListener("click", showMapView);
    statusTab.addEventListener("click", showStatusView);

    const map = L.map("map", {{ zoomControl: true }});
    L.tileLayer("https://tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png", {{
      maxZoom: 19,
      attribution: "&copy; OpenStreetMap contributors"
    }}).addTo(map);

    const markers = [];

    function providerRowsHtml(feature) {{
      const rows = [];
      for (const item of feature.properties.available_providers || []) {{
        const provider = item.provider;
        const details = feature.properties.provider_details[provider] || {{}};
        const score = item.score || details.score || "Unknown";
        rows.push(`
          <tr>
            <td>${{provider}}</td>
            <td><strong style="color:${{scoreColors[score] || scoreColors.Unknown}}">${{score}}</strong></td>
            <td>${{details.nearest_tower_km || "NA"}}</td>
            <td>${{details.tower_count || "NA"}}</td>
            <td>${{details.strongest_signal_dbm || "NA"}}</td>
          </tr>
        `);
      }}
      return rows.length
        ? rows.join("")
        : `<tr><td colspan="5">No provider with confirmed availability in current data.</td></tr>`;
    }}

    function popupHtml(feature) {{
      const villageLabel = feature.properties.display_village || feature.properties.village;
      const proxyNote = feature.properties.is_proxy
        ? '<div style="margin-top:6px;padding:6px 8px;border-radius:8px;background:#f5f1e5;border:1px solid #ddd5bf;font-size:11px;">District proxy anchor: this point is used because no village-master coordinates were available.</div>'
        : '';
      return `
        <div>
          <strong>${{villageLabel}}</strong><br/>
          State: ${{feature.properties.state}}<br/>
          District: ${{feature.properties.district}}<br/>
          Block: ${{feature.properties.block}}<br/>
          LGD Code: ${{feature.properties.lgd_code}}<br/>
          Coordinate source: ${{feature.properties.coordinate_source}}
          ${{proxyNote}}
          <table class="popup-table">
            <tr><td><strong>Provider</strong></td><td><strong>Score</strong></td><td><strong>Nearest tower km</strong></td><td><strong>Towers</strong></td><td><strong>Best dBm</strong></td></tr>
            ${{providerRowsHtml(feature)}}
          </table>
        </div>
      `;
    }}

    for (const feature of geojson.features) {{
      const [lon, lat] = feature.geometry.coordinates;
      if (!feature.properties.has_valid_coordinates || !Number.isFinite(lat) || !Number.isFinite(lon)) {{
        continue;
      }}
      const available = feature.properties.available_providers || [];
      const lettersHtml = available.length
        ? available.map((item) => `<span class="provider-letter" style="color:${{scoreColors[item.score] || scoreColors.Unknown}}">${{item.label}}</span>`).join("")
        : `<span class="provider-letter" style="color:${{scoreColors.Unknown}}">?</span>`;
      const proxyClass = feature.properties.is_proxy ? " provider-box-proxy" : "";
      const marker = L.marker([lat, lon], {{
        icon: L.divIcon({{
          className: "provider-marker-wrapper",
          html: `<span class="provider-box${{proxyClass}}">${{lettersHtml}}</span>`,
          iconSize: [30, 30],
          iconAnchor: [15, 15],
        }})
      }});
      marker.feature = feature;
      marker.availableScores = new Set(available.map((item) => item.score));
      marker.hasAvailableProvider = available.length > 0;
      marker.bindPopup(popupHtml(feature));
      marker.bindTooltip(feature.properties.display_village || feature.properties.village, {{ direction: "top", className: "provider-label" }});
      markers.push(marker);
    }}

    function markerMatches(marker) {{
      const feature = marker.feature;
      const matchesScore = marker.hasAvailableProvider
        ? Array.from(marker.availableScores).some((score) => selectedScores.has(score))
        : selectedScores.has("Unknown");
      const matchesState = !selectedState || feature.properties.state === selectedState;
      const matchesDistrict = !selectedDistrict || feature.properties.district === selectedDistrict;
      const matchesSearch = !searchText || feature.properties.village.toLowerCase().includes(searchText);
      return matchesScore && matchesState && matchesDistrict && matchesSearch;
    }}

    function refreshMarkers(fitBounds = false) {{
      const visibleVillageIds = new Set();
      const visibleBounds = [];
      for (const marker of markers) {{
        const visible = markerMatches(marker);
        if (visible) {{
          if (!map.hasLayer(marker)) {{
            marker.addTo(map);
          }}
          visibleVillageIds.add(marker.feature.properties.village_id);
          visibleBounds.push(marker.getLatLng());
        }} else if (map.hasLayer(marker)) {{
          map.removeLayer(marker);
        }}
      }}
      visibleCount.textContent = String(visibleVillageIds.size);
      if (fitBounds && visibleBounds.length) {{
        map.fitBounds(L.latLngBounds(visibleBounds), {{ padding: [30, 30] }});
      }}
    }}

    fillDistrictOptions();
    refreshDistrictStatusList();
    refreshStatusTable();
    refreshMarkers(true);
  </script>
</body>
</html>
"""


def main() -> None:
    summary_df = pd.read_csv(SUMMARY_CSV)
    status_df = pd.read_csv(STATUS_CSV)
    summary_payload = json.loads(SUMMARY_JSON.read_text(encoding="utf-8")) if SUMMARY_JSON.exists() else {}
    missing_districts = summary_payload.get("missing_districts", [])
    features = load_combined_features(summary_df)
    html = build_html(summary_df, status_df, missing_districts, features)
    OUTPUT_HTML.parent.mkdir(parents=True, exist_ok=True)
    archive_existing_path(OUTPUT_HTML)
    OUTPUT_HTML.write_text(html, encoding="utf-8")
    print(f"Wrote combined requested district site to {OUTPUT_HTML}")


if __name__ == "__main__":
    main()
