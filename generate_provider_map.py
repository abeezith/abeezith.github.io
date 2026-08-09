from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import pandas as pd

from archive_utils import archive_existing_path


ROOT = Path(__file__).resolve().parent
DEFAULT_INPUT_CSV = ROOT / "outputs" / "village_provider_signal_estimate.csv"
DEFAULT_OUTPUT_HTML = ROOT / "outputs" / "village_connectivity_map.html"

SCORE_ORDER = ["Strong", "Moderate", "Weak", "Unknown"]
SCORE_COLORS = {
    "Strong": "#1a9850",
    "Moderate": "#fee08b",
    "Weak": "#f46d43",
    "Unknown": "#9aa0a6",
}
PROVIDER_STYLES = {
    "Airtel": {"label": "A"},
    "BSNL": {"label": "B"},
    "Jio": {"label": "J"},
    "Vodafone Idea": {"label": "V"},
}
PROVIDER_ORDER = ["Airtel", "BSNL", "Jio", "Vodafone Idea"]


def safe_number(value: object, digits: int = 2) -> str:
    if pd.isna(value):
        return ""
    return f"{float(value):.{digits}f}"


def is_valid_coordinate(latitude: object, longitude: object) -> bool:
    if pd.isna(latitude) or pd.isna(longitude):
        return False
    lat = float(latitude)
    lon = float(longitude)
    return math.isfinite(lat) and math.isfinite(lon)


def build_feature_rows(df: pd.DataFrame) -> tuple[list[str], list[dict[str, object]]]:
    providers = [provider for provider in PROVIDER_ORDER if provider in set(df["provider"].dropna().astype(str).unique().tolist())]
    features: list[dict[str, object]] = []

    for village_id, group in df.groupby("village_id", sort=True):
        first = group.iloc[0]
        provider_details: dict[str, dict[str, object]] = {}
        available_providers: list[dict[str, str]] = []
        for row in group.itertuples(index=False):
            row_data = row._asdict()
            score = str(row_data.get("coverage_score"))
            if score == "Unknown":
                continue
            provider_name = str(row_data.get("provider"))
            provider_details[provider_name] = {
                "score": score,
                "nearest_tower_km": safe_number(row_data.get("nearest_tower_km")),
                "tower_count": "" if pd.isna(row_data.get("tower_count")) else int(row_data.get("tower_count")),
                "strongest_signal_dbm": safe_number(row_data.get("strongest_signal_dbm"), 0),
                "ookla_download_mbps": safe_number(row_data.get("village_ookla_download_mbps")),
                "ookla_upload_mbps": safe_number(row_data.get("village_ookla_upload_mbps")),
                "ookla_tests": "" if pd.isna(row_data.get("village_ookla_tests")) else int(row_data.get("village_ookla_tests")),
                "ookla_distance_km": safe_number(row_data.get("village_ookla_distance_km")),
                "note": str(row_data.get("assessment_note") or ""),
            }
            available_providers.append(
                {
                    "provider": provider_name,
                    "score": score,
                    "label": PROVIDER_STYLES.get(provider_name, {"label": "?"})["label"],
                }
            )

        features.append(
            {
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [
                        float(first["longitude"]) if is_valid_coordinate(first["latitude"], first["longitude"]) else None,
                        float(first["latitude"]) if is_valid_coordinate(first["latitude"], first["longitude"]) else None,
                    ],
                },
                "properties": {
                    "village_id": int(village_id),
                    "state": str(first["state"]),
                    "district": str(first["district"]),
                    "block": str(first["block"]),
                    "village": str(first["village"]),
                    "display_village": str(first.get("display_village") or first["village"]),
                    "lgd_code": str(first["lgd_code"]),
                    "coordinate_source": str(first["coordinate_source"]),
                    "is_proxy": bool(first.get("is_proxy")) or str(first.get("coordinate_source")) in {"district_proxy", "source_proxy"},
                    "has_valid_coordinates": is_valid_coordinate(first["latitude"], first["longitude"]),
                    "available_providers": available_providers,
                    "provider_details": provider_details,
                },
            }
        )

    return providers, features


def build_html(providers: list[str], features: list[dict[str, object]]) -> str:
    district_name = "selected district"
    if features:
        district_name = str(features[0]["properties"].get("district") or district_name)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Village Connectivity Map</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
  <style>
    body {{
      margin: 0;
      font-family: Georgia, "Times New Roman", serif;
      background: linear-gradient(180deg, #f8f5ed 0%, #eef2e3 100%);
      color: #1f2a1f;
    }}
    #app {{
      display: grid;
      grid-template-columns: 390px 1fr;
      min-height: 100vh;
    }}
    #sidebar {{
      padding: 20px 18px;
      border-right: 1px solid #d7ddc8;
      background: rgba(255, 252, 246, 0.94);
      overflow-y: auto;
    }}
    #map {{
      height: 100vh;
      width: 100%;
    }}
    h1 {{
      margin: 0 0 8px;
      font-size: 26px;
      line-height: 1.1;
    }}
    .muted {{
      color: #4f5c4f;
      font-size: 14px;
      margin-bottom: 16px;
    }}
    .control {{
      margin-bottom: 18px;
    }}
    .control label {{
      display: block;
      margin-bottom: 6px;
      font-weight: 700;
      font-size: 13px;
      letter-spacing: 0.04em;
      text-transform: uppercase;
    }}
    input[type="text"] {{
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
    .legend {{
      margin-top: 16px;
      padding: 12px;
      border-radius: 12px;
      background: #f5f1e5;
      border: 1px solid #ddd5bf;
      font-size: 13px;
    }}
    .stat {{
      margin-top: 18px;
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
  </style>
</head>
<body>
  <div id="app">
    <aside id="sidebar">
      <h1>Village Connectivity Map</h1>
      <div class="muted">Interactive view of {district_name} district village connectivity estimates. Each village is shown as one box, and only available networks are listed inside it as colored letters.</div>

      <div class="control">
        <label for="villageSearch">Village Search</label>
        <input id="villageSearch" type="text" placeholder="Type a village name" />
      </div>

      <div class="control">
        <label>Visible Scores</label>
        <div class="checks" id="scoreFilters"></div>
      </div>

      <div class="legend">
        <div><strong>Color Guide</strong></div>
        <div>Strong = dark green</div>
        <div>Moderate = sand yellow</div>
        <div>Weak = orange-red</div>
        <div>Unknown = grey</div>
      </div>

      <div class="legend">
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
    </aside>
    <div id="map"></div>
  </div>

  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <script>
    const providers = {json.dumps(providers)};
    const scoreOrder = {json.dumps(SCORE_ORDER)};
    const scoreColors = {json.dumps(SCORE_COLORS)};
    const providerStyles = {json.dumps(PROVIDER_STYLES)};
    const geojson = {json.dumps({"type": "FeatureCollection", "features": features})};

    const villageSearch = document.getElementById("villageSearch");
    const scoreFilters = document.getElementById("scoreFilters");
    const visibleCount = document.getElementById("visibleCount");

    let selectedScores = new Set(scoreOrder);
    let searchText = "";

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

    villageSearch.addEventListener("input", () => {{
      searchText = villageSearch.value.trim().toLowerCase();
      refreshMarkers();
    }});

    const map = L.map("map", {{ zoomControl: true }});
    L.tileLayer("https://tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png", {{
      maxZoom: 19,
      attribution: "&copy; OpenStreetMap contributors"
    }}).addTo(map);

    const markers = [];
    const bounds = [];

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

    function availableProviders(feature) {{
      return feature.properties.available_providers || [];
    }}

    for (const feature of geojson.features) {{
      const [lon, lat] = feature.geometry.coordinates;
      if (!feature.properties.has_valid_coordinates || !Number.isFinite(lat) || !Number.isFinite(lon)) {{
        continue;
      }}
      const available = availableProviders(feature);
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
      bounds.push([lat, lon]);
    }}

    function refreshMarkers() {{
      const visibleVillageIds = new Set();
      for (const marker of markers) {{
        const feature = marker.feature;
        const matchesScore = marker.hasAvailableProvider
          ? Array.from(marker.availableScores).some((score) => selectedScores.has(score))
          : selectedScores.has("Unknown");
        const matchesSearch = !searchText || feature.properties.village.toLowerCase().includes(searchText);
        const visible = matchesScore && matchesSearch;

        if (visible) {{
          if (!map.hasLayer(marker)) {{
            marker.addTo(map);
          }}
          visibleVillageIds.add(feature.properties.village_id);
        }} else if (map.hasLayer(marker)) {{
          map.removeLayer(marker);
        }}
      }}
      visibleCount.textContent = String(visibleVillageIds.size);
    }}

    if (bounds.length) {{
      map.fitBounds(bounds, {{ padding: [30, 30] }});
    }} else {{
      map.setView([18.81, 82.71], 10);
    }}

    refreshMarkers();
  </script>
</body>
</html>
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate an interactive provider map from a provider score CSV.")
    parser.add_argument("--input-csv", type=Path, default=DEFAULT_INPUT_CSV, help="Path to village_provider_signal_estimate.csv")
    parser.add_argument("--output-html", type=Path, default=DEFAULT_OUTPUT_HTML, help="Path to write the HTML map")
    args = parser.parse_args()

    input_csv = args.input_csv.resolve()
    output_html = args.output_html.resolve()

    df = pd.read_csv(input_csv)
    providers, features = build_feature_rows(df)
    html = build_html(providers, features)
    output_html.parent.mkdir(parents=True, exist_ok=True)
    archive_existing_path(output_html)
    output_html.write_text(html, encoding="utf-8")
    print(f"Wrote interactive map to {output_html}")


if __name__ == "__main__":
    main()
