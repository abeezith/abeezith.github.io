from __future__ import annotations

import gzip
import json
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import pandas as pd
import requests
from pyproj import Transformer
from shapely.geometry import shape


ROOT = Path(__file__).resolve().parent
SOURCE_CSV = Path(r"E:\Resources\SecondBrain\odisha_gp_village_to_subcentre_mapping_data.csv")
DATA_DIR = ROOT / "data"
VILLAGES_XLSX = DATA_DIR / "villages.xlsx"
MASTER_XLSX = DATA_DIR / "lgd_village_master.xlsx"
OPENCELLID_GZ = DATA_DIR / "opencellid_india.csv.gz"
OOKLA_GEOJSON = DATA_DIR / "ookla_mobile_tiles.geojson"

BLOCK_CODE = "0803"
DISTRICT_CODE = "120"
DISTRICT_NAME = "Koraput"

GP_ALIASES = {
    "badasuku": "suku",
    "devighat": "deoghati",
    "dumuripadar": "dumuripadara",
    "m arichmal": "marichamala",
    "manbar": "manabar",
}

MANUAL_VILLAGE_ALIASES = {
    ("suku", "horidaput"): "hariraput",
    ("suku", "sirishi"): "sirsi",
    ("deoghati", "devighat"): "deoghati",
    ("deoghati", "bogeipodar"): "baghaipadar",
    ("deoghati", "doudapadar"): "daurapadar",
    ("dumuripadara", "challor"): "cholar",
    ("dumuripadara", "dumuripadar"): "dumripadar",
    ("dumuripadara", "janjanaguda"): "janjanagura",
    ("dumuripadara", "keragam"): "keregam",
    ("dumuripadara", "khagadora"): "khagodara",
    ("dumuripadara", "palijodipodar"): "palijaripadar",
    ("dumuripadara", "pendajam"): "pendajamu",
    ("kendar", "bondakotra"): "bandakatra",
    ("kerenga", "kerenga"): "karnga",
    ("lankaput", "kholap"): "khalap",
    ("litiguda", "amalabadi"): "anlabari",
    ("litiguda", "lachamani"): "lachhuani",
    ("mahadeiput", "dolaiput"): "daleiput",
    ("mahadeiput", "doliamba"): "daliam",
    ("mahadeiput", "ekdili"): "ekdali",
    ("mahadeiput", "machhara ii"): "machhra",
    ("manabar", "jerty"): "jarti",
    ("mastiput", "dongri"): "dunguri",
    ("mastiput", "rudhiamba"): "rundiamb",
    ("mastiput", "tola"): "tala",
    ("padmapur", "chougaon"): "chhaagan",
    ("padmapur", "mohanapada"): "mahanpara",
    ("padmapur", "nighamaniguda"): "nigamangura",
    ("umuri", "machhara i"): "machhra",
    ("umuri", "padeiguda"): "parheigura",
    (None, "damanjodi"): "dhamanjori",
    (None, "kanheiput"): "kahnaiput",
    ("koraput nac", "dangadeula"): "dangdeula",
    ("koraput nac", "disari kharaguda"): "disarikharagura",
    ("koraput nac", "koraput nagar"): "koraputnagara",
    ("koraput nac", "landiguda"): "landigura",
    ("koraput nac", "tentuliguda"): "tentuligura",
    ("damanjodi nac", "kantaguda"): "kantagura",
    ("damanjodi nac", "damanjodi"): "dhamanjori",
}


def normalize(text: Any) -> str:
    cleaned = str(text or "").strip().lower()
    cleaned = cleaned.replace("&", "and")
    cleaned = re.sub(r"\([^)]*\)", " ", cleaned)
    cleaned = re.sub(r"[^a-z0-9]+", " ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def normalize_match_key(text: Any) -> str:
    cleaned = normalize(text)
    replacements = {
        "gh": "g",
        "kh": "k",
        "bh": "b",
        "dh": "d",
        "jh": "j",
        "ph": "f",
        "ct": "",
        "nac": "",
    }
    for source, target in replacements.items():
        cleaned = cleaned.replace(source, target)
    cleaned = cleaned.replace("ii", "2").replace("iii", "3").replace("iv", "4").replace(" i ", " 1 ")
    cleaned = re.sub(r"\bgura\b", "guda", cleaned)
    cleaned = re.sub(r"\bguri\b", "gudi", cleaned)
    cleaned = re.sub(r"\bjori\b", "jodi", cleaned)
    cleaned = re.sub(r"\bnagara\b", "nagar", cleaned)
    cleaned = re.sub(r"\bpadara\b", "padar", cleaned)
    cleaned = re.sub(r"\bpodar\b", "padar", cleaned)
    cleaned = re.sub(r"\bputra\b", "putra", cleaned)
    cleaned = re.sub(r"\bput\b", "put", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def fetch_json(session: requests.Session, url: str, data: dict[str, Any]) -> Any:
    response = session.post(url, data=data, timeout=60)
    response.raise_for_status()
    return response.json()


def build_master() -> pd.DataFrame:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    transformer = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)

    villages = pd.read_csv(SOURCE_CSV)
    villages = villages[
        (villages["LGD_District_Name"] == "Koraput") & (villages["LGD_Subdistrict_Name"] == "Koraput")
    ].copy()
    villages["state"] = "Odisha"
    villages["district"] = villages["LGD_District_Name"]
    villages["block"] = villages["LGD_Subdistrict_Name"]
    villages["village"] = villages["Village_Name"]
    villages["lgd_code"] = villages["Village_Code"].astype(str).str.strip()
    villages["gp_name_norm"] = villages["GP_Name"].map(normalize)
    villages["village_norm"] = villages["village"].map(normalize)
    villages["village_match_key"] = villages["village"].map(normalize_match_key)

    gp_rows = fetch_json(
        session,
        "https://odisha4kgeo.in/index.php/mapview/getRevenueGP",
        {"blocks": BLOCK_CODE, "district": DISTRICT_NAME},
    )
    gp_lookup = {normalize(row["grampanchayat_name"]): row for row in gp_rows}

    block_village_index: dict[str, list[dict[str, str]]] = {}
    block_match_index: dict[str, list[dict[str, str]]] = {}
    gp_village_index: dict[str, list[dict[str, str]]] = {}
    gp_match_index: dict[str, list[dict[str, str]]] = {}
    matched_gp_codes: set[str] = set()
    for gp_norm, row in gp_lookup.items():
        village_rows = fetch_json(
            session,
            "https://odisha4kgeo.in/index.php/mapview/getVillage",
            {"lulcgp": row["grampanchayat_code"], "blocks": BLOCK_CODE},
        )
        for village_row in village_rows:
            candidate = {
                "gp_name_norm": gp_norm,
                "gp_code": row["grampanchayat_code"],
                "service_village_name": village_row["revenue_village_name"],
                "service_village_code": village_row["revenue_village_code"],
                "service_village_norm": normalize(village_row["revenue_village_name"]),
                "service_match_key": normalize_match_key(village_row["revenue_village_name"]),
            }
            block_village_index.setdefault(candidate["service_village_norm"], []).append(candidate)
            block_match_index.setdefault(candidate["service_match_key"], []).append(candidate)
            gp_village_index.setdefault(gp_norm, []).append(candidate)
            gp_match_index.setdefault(gp_norm, []).append(candidate)

    extent_cache: dict[tuple[str, str], tuple[float, float]] = {}
    master_rows: list[dict[str, Any]] = []
    unresolved_rows: list[dict[str, Any]] = []

    for row in villages.to_dict("records"):
        gp_norm = row["gp_name_norm"]
        gp_norm = GP_ALIASES.get(gp_norm, gp_norm)
        resolved_gp = gp_lookup.get(gp_norm)
        village_candidates = block_village_index.get(row["village_norm"], [])
        village_match_candidates = block_match_index.get(row["village_match_key"], [])

        selected: dict[str, str] | None = None
        if resolved_gp:
            selected = select_manual_alias(gp_norm, row["village_norm"], gp_match_index.get(gp_norm, []))
        if resolved_gp:
            matched_gp_codes.add(resolved_gp["grampanchayat_code"])
            if selected is None:
                for candidate in village_candidates + village_match_candidates:
                    if candidate["gp_code"] == resolved_gp["grampanchayat_code"]:
                        selected = candidate
                        break
            if selected is None:
                selected = best_fuzzy_match(row["village_norm"], gp_village_index.get(gp_norm, []))
            if selected is None:
                selected = best_match_key_fuzzy(row["village_match_key"], gp_match_index.get(gp_norm, []))

        if selected is None and len(village_candidates) == 1:
            selected = village_candidates[0]
        if selected is None and len(village_match_candidates) == 1:
            selected = village_match_candidates[0]
        if selected is None:
            selected = select_manual_alias(None, row["village_norm"], list(block_match_index.get(row["village_match_key"], [])) or sum(gp_match_index.values(), []))
        if selected is None:
            selected = best_fuzzy_match(row["village_norm"], village_candidates)
        if selected is None:
            selected = best_match_key_fuzzy(row["village_match_key"], village_match_candidates)
        if selected is None and pd.isna(row["GP_Name"]):
            selected = best_match_key_fuzzy(row["village_match_key"], sum(gp_match_index.values(), []), min_score=0.88, min_gap=0.08)

        if selected is None:
            unresolved_rows.append(
                {
                    "state": row["state"],
                    "district": row["district"],
                    "block": row["block"],
                    "village": row["village"],
                    "lgd_code": row["lgd_code"],
                    "gp_name": row["GP_Name"],
                }
            )
            continue

        cache_key = (selected["gp_code"], selected["service_village_code"])
        if cache_key not in extent_cache:
            extent_payload = fetch_json(
                session,
                "https://odisha4kgeo.in/index.php/mapview/getVillageExtent",
                {"lulcvillage": selected["service_village_code"], "lulcgp": selected["gp_code"]},
            )
            geom = shape(json.loads(extent_payload["geojson"]))
            centroid_x, centroid_y = geom.centroid.x, geom.centroid.y
            lon, lat = transformer.transform(centroid_x, centroid_y)
            extent_cache[cache_key] = (lat, lon)

        lat, lon = extent_cache[cache_key]
        master_rows.append(
            {
                "state": row["state"],
                "district": row["district"],
                "block": row["block"],
                "village": row["village"],
                "lgd_code": row["lgd_code"],
                "latitude": lat,
                "longitude": lon,
                "gp_name": row["GP_Name"],
                "service_gp_code": selected["gp_code"],
                "service_village_code": selected["service_village_code"],
                "service_village_name": selected["service_village_name"],
                "coordinate_source": "odisha_kyl",
            }
        )

    if unresolved_rows:
        unresolved_df = pd.DataFrame(unresolved_rows).sort_values(["gp_name", "village"])
        unresolved_path = DATA_DIR / "koraput_unresolved_villages.csv"
        try:
            unresolved_df.to_csv(unresolved_path, index=False)
            print(f"Wrote unresolved village list to {unresolved_path}")
        except PermissionError:
            fallback_path = DATA_DIR / "koraput_unresolved_villages.latest.csv"
            unresolved_df.to_csv(fallback_path, index=False)
            print(f"Wrote unresolved village list to {fallback_path}")

    if not master_rows:
        raise RuntimeError("No Koraput block villages could be resolved from Odisha KYL.")

    master_df = pd.DataFrame(master_rows).sort_values(["gp_name", "village"]).reset_index(drop=True)
    return master_df


def write_pipeline_inputs(master_df: pd.DataFrame) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    village_input_df = master_df[["state", "district", "block", "village", "lgd_code"]].copy()
    village_input_df["latitude"] = pd.NA
    village_input_df["longitude"] = pd.NA

    village_input_df.to_excel(VILLAGES_XLSX, index=False)
    master_df[["state", "district", "block", "village", "lgd_code", "latitude", "longitude"]].to_excel(
        MASTER_XLSX, index=False
    )

    with gzip.open(OPENCELLID_GZ, "wt", encoding="utf-8", newline="") as handle:
        handle.write("radio,mcc,mnc,lac,cellid,lat,lon,operator\n")

    OOKLA_GEOJSON.write_text(
        json.dumps({"type": "FeatureCollection", "features": []}, indent=2),
        encoding="utf-8",
    )


def best_fuzzy_match(village_norm: str, candidates: list[dict[str, str]]) -> dict[str, str] | None:
    if not candidates:
        return None

    scored: list[tuple[float, dict[str, str]]] = []
    for candidate in candidates:
        score = SequenceMatcher(None, village_norm, normalize(candidate["service_village_name"])).ratio()
        scored.append((score, candidate))

    scored.sort(key=lambda item: item[0], reverse=True)
    top_score, top_candidate = scored[0]
    second_score = scored[1][0] if len(scored) > 1 else 0.0
    if top_score >= 0.84 and top_score - second_score >= 0.05:
        return top_candidate
    return None


def best_match_key_fuzzy(
    village_match_key: str,
    candidates: list[dict[str, str]],
    min_score: float = 0.8,
    min_gap: float = 0.06,
) -> dict[str, str] | None:
    if not candidates:
        return None

    scored: list[tuple[float, dict[str, str]]] = []
    for candidate in candidates:
        score = SequenceMatcher(None, village_match_key, candidate["service_match_key"]).ratio()
        scored.append((score, candidate))

    scored.sort(key=lambda item: item[0], reverse=True)
    top_score, top_candidate = scored[0]
    second_score = scored[1][0] if len(scored) > 1 else 0.0
    if top_score >= min_score and top_score - second_score >= min_gap:
        return top_candidate
    return None


def select_manual_alias(
    gp_norm: str | None,
    village_norm: str,
    candidates: list[dict[str, str]],
) -> dict[str, str] | None:
    keys = []
    if gp_norm is not None:
        keys.append((gp_norm, village_norm))
    keys.append((None, village_norm))

    for key in keys:
        alias_target = MANUAL_VILLAGE_ALIASES.get(key)
        if not alias_target:
            continue
        for candidate in candidates:
            if candidate["service_village_norm"] == alias_target:
                return candidate
    return None


def main() -> None:
    master_df = build_master()
    write_pipeline_inputs(master_df)
    print(f"Wrote {len(master_df)} village master rows to {MASTER_XLSX}")
    print(f"Wrote pipeline village input template to {VILLAGES_XLSX}")
    print(f"Wrote empty OpenCellID placeholder to {OPENCELLID_GZ}")
    print(f"Wrote empty Ookla placeholder to {OOKLA_GEOJSON}")


if __name__ == "__main__":
    main()
