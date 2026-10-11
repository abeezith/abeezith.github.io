"""Build a compact public history page from retained weekly and monthly rollups."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parent
RETENTION_ROOT = ROOT / "outputs" / "retention"
OUTPUT = ROOT / "outputs" / "requested_districts_site" / "history.html"
SCORE_RANK = {"Unknown": 0, "Weak": 1, "Moderate": 2, "Strong": 3}


def score_label(value: float) -> str:
    if value >= 2.5:
        return "Strong"
    if value >= 1.5:
        return "Moderate"
    if value >= 0.5:
        return "Weak"
    return "Unknown"


def weekly_rows() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for path in sorted((RETENTION_ROOT / "weekly").glob("*/village_provider_signal_estimate.csv")):
        frame = pd.read_csv(path, low_memory=False)
        if frame.empty:
            continue
        frame["rank"] = frame["coverage_score"].map(SCORE_RANK).fillna(0)
        grouped = frame.groupby(["state", "district", "provider"], dropna=False).agg(
            score_rank=("rank", "mean"),
            nearest_tower_km=("nearest_tower_km", "mean"),
            village_count=("village_id", "nunique"),
        ).reset_index()
        for item in grouped.to_dict("records"):
            item["period"] = path.parent.name
            item["strength"] = score_label(float(item["score_rank"]))
            item["nearest_tower_km"] = round(float(item["nearest_tower_km"]), 2) if pd.notna(item["nearest_tower_km"]) else None
            item["score_rank"] = round(float(item["score_rank"]), 2)
            item["village_count"] = int(item["village_count"])
            rows.append(item)
    return rows


def monthly_rows() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for path in sorted((RETENTION_ROOT / "monthly").glob("*/monthly_district_provider_rollup.csv")):
        frame = pd.read_csv(path, low_memory=False)
        for item in frame.to_dict("records"):
            item["period"] = path.parent.name
            rank = pd.to_numeric(item.get("coverage_rank_mean"), errors="coerce")
            item["score_rank"] = round(float(rank), 2) if pd.notna(rank) else 0.0
            item["strength"] = score_label(float(item["score_rank"]))
            value = pd.to_numeric(item.get("nearest_tower_km_mean"), errors="coerce")
            item["nearest_tower_km"] = round(float(value), 2) if pd.notna(value) else None
            village_count = pd.to_numeric(item.get("village_count"), errors="coerce")
            item["village_count"] = int(village_count) if pd.notna(village_count) else 0
            rows.append(item)
    return rows


def build_html(weekly: list[dict[str, object]], monthly: list[dict[str, object]]) -> str:
    payload = json.dumps({"weekly": weekly, "monthly": monthly})
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Connectivity Strength History</title><style>
body{{margin:0;background:#f7f3e8;color:#223127;font-family:Georgia,serif}}main{{max-width:1120px;margin:auto;padding:28px 20px 48px}}h1{{margin:0 0 8px}}.muted{{color:#53604e}}.filters{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin:24px 0}}select{{padding:10px;border:1px solid #b7c0a4;border-radius:8px;background:#fffef9;font:inherit}}.tabs{{display:flex;gap:8px;margin:20px 0}}button{{padding:9px 13px;border:1px solid #b7c0a4;border-radius:8px;background:#fffef9;font:inherit;font-weight:bold;cursor:pointer}}button.active{{background:#223127;color:#fff}}table{{width:100%;border-collapse:collapse;background:#fffdf8}}th,td{{text-align:left;padding:10px;border-bottom:1px solid #ddd5bf;font-size:14px}}.Strong{{color:#198754;font-weight:bold}}.Moderate{{color:#a66c00;font-weight:bold}}.Weak{{color:#c74a2f;font-weight:bold}}.Unknown{{color:#68717a;font-weight:bold}}a{{color:#223127}}@media(max-width:700px){{.filters{{grid-template-columns:1fr}}th,td{{font-size:12px;padding:7px}}}}</style></head>
<body><main><a href="index.html">&larr; Map and status</a><h1>Network Strength History</h1><p class="muted">Weekly snapshots retain the latest four runs. Monthly values are district/provider averages retained for twelve months. Strength is an estimate from current OpenCellID tower evidence and the linked Ookla release.</p>
<div class="filters"><select id="state"><option value="">All states</option></select><select id="district"><option value="">All districts</option></select><select id="provider"><option value="">All providers</option></select></div><div class="tabs"><button data-view="weekly" class="active">Last 4 weeks</button><button data-view="monthly">Monthly average</button></div><p id="note" class="muted"></p><table><thead><tr><th>Period</th><th>State</th><th>District</th><th>Provider</th><th>Strength</th><th>Mean nearest tower</th><th>Villages</th></tr></thead><tbody id="rows"></tbody></table></main>
<script>const data={payload};let view='weekly';const state=document.querySelector('#state'),district=document.querySelector('#district'),provider=document.querySelector('#provider'),body=document.querySelector('#rows'),note=document.querySelector('#note');const unique=(a)=>[...new Set(a)].sort();function option(el,items,label){{const keep=el.value;el.innerHTML='<option value="">'+label+'</option>'+items.map(x=>`<option>${{x}}</option>`).join('');el.value=items.includes(keep)?keep:''}}function refreshOptions(){{const rows=data[view];option(state,unique(rows.map(x=>x.state)),'All states');option(district,unique(rows.filter(x=>!state.value||x.state===state.value).map(x=>x.district)),'All districts');option(provider,unique(rows.filter(x=>(!state.value||x.state===state.value)&&(!district.value||x.district===district.value)).map(x=>x.provider)),'All providers')}}function render(){{refreshOptions();const rows=data[view].filter(x=>(!state.value||x.state===state.value)&&(!district.value||x.district===district.value)&&(!provider.value||x.provider===provider.value)).sort((a,b)=>String(b.period).localeCompare(String(a.period)));note.textContent=rows.length?`${{rows.length}} district/provider periods shown.`:'No retained data is available yet; the first successful weekly refresh will populate this view.';body.innerHTML=rows.map(x=>`<tr><td>${{x.period}}</td><td>${{x.state}}</td><td>${{x.district}}</td><td>${{x.provider}}</td><td class="${{x.strength}}">${{x.strength}}</td><td>${{x.nearest_tower_km==null?'—':x.nearest_tower_km+' km'}}</td><td>${{x.village_count}}</td></tr>`).join('')}}[state,district,provider].forEach(el=>el.addEventListener('change',render));document.querySelectorAll('button[data-view]').forEach(b=>b.addEventListener('click',()=>{{view=b.dataset.view;document.querySelectorAll('button[data-view]').forEach(x=>x.classList.toggle('active',x===b));render()}}));render();</script></body></html>"""


def main() -> None:
    weekly, monthly = weekly_rows(), monthly_rows()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(build_html(weekly, monthly), encoding="utf-8")
    print(f"Wrote {OUTPUT} with {len(weekly)} weekly and {len(monthly)} monthly rows.")


if __name__ == "__main__":
    main()
