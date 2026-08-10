"""Gold outputs -> GeoJSONL for the web map's vector tiles.

Three layers, one file each, consumed by scripts/make_tiles.sh:

    hexes    every receiver cell, carrying everything the click panel needs
             to answer "why is signal weak HERE": predicted RSRP, population,
             the serving tower (id, height, distance, line of sight), and the
             environment (tree cover, relief, building heights) from the
             Sedona zonal-stats stage.
    towers   ASR structures with their registered heights.
    sites    the optimizer's recommended builds, ranked.

Plain pandas, no Spark: this runs after the pipeline, must work with the
cluster gone, and 3k-85k rows is not a distributed problem.

Usage:
    SCOPE=demo LOCAL_OUT=1 python scripts/make_web_data.py
"""
import glob
import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from config import REPO_ROOT, RF  # noqa: E402
from session import out_path  # noqa: E402

OUT_DIR = REPO_ROOT / "web" / "data"


def read_dir(path: str) -> pd.DataFrame:
    files = sorted(glob.glob(f"{path}/*.parquet"))
    if not files:
        raise FileNotFoundError(f"no parquet under {path}; run `make demo` first")
    return pd.concat((pd.read_parquet(f) for f in files), ignore_index=True)


def feature(geom: dict, props: dict) -> str:
    # Nulls are dropped rather than serialised: tippecanoe stores them as the
    # string "null" otherwise, and the panel JS treats absence as "no data".
    clean = {k: v for k, v in props.items() if v is not None and v == v}
    return json.dumps({"type": "Feature", "geometry": geom,
                       "properties": clean}, separators=(",", ":"))


def hex_layer() -> int:
    import h3

    cov = read_dir(out_path("gold", "coverage"))
    feats = read_dir(out_path("silver", "hex_features"))[
        ["h3_r8", "elev_mean_m", "relief_m", "tree_frac", "built_frac",
         "bldg_mean_m", "bldg_max_m"]]
    towers = read_dir(out_path("bronze", "towers"))[["asr_id", "height_agl_m"]]

    df = cov.merge(feats, on="h3_r8", how="left").merge(
        towers.rename(columns={"asr_id": "best_asr_id",
                               "height_agl_m": "srv_height_m"}),
        on="best_asr_id", how="left")

    with open(OUT_DIR / "hexes.geojsonl", "w") as fh:
        for r in df.itertuples(index=False):
            # cell_to_boundary returns (lat, lng); GeoJSON wants (lng, lat).
            ring = [[lng, lat] for lat, lng in h3.cell_to_boundary(r.h3_str)]
            ring.append(ring[0])
            fh.write(feature(
                {"type": "Polygon", "coordinates": [ring]},
                {
                    "h3": r.h3_str,
                    "rsrp": round(r.best_rsrp_dbm, 1)
                            if pd.notna(r.best_rsrp_dbm) else None,
                    "covered": bool(r.is_covered),
                    "pop": round(r.pop, 1),
                    "demand": round(r.demand),
                    "n_srv": int(r.n_servers),
                    "srv_asr": r.best_asr_id,
                    "srv_km": round(r.best_distance_m / 1000.0, 1)
                              if pd.notna(r.best_distance_m) else None,
                    "srv_los": bool(r.best_is_los)
                               if pd.notna(r.best_is_los) else None,
                    "srv_h": round(r.srv_height_m)
                             if pd.notna(r.srv_height_m) else None,
                    "tree": round(r.tree_frac * 100.0),
                    "built": round(r.built_frac * 100.0),
                    "relief": round(r.relief_m),
                    "bldg_mean": round(r.bldg_mean_m, 1),
                    "bldg_max": round(r.bldg_max_m),
                }) + "\n")
    return len(df)


def tower_layer() -> int:
    t = read_dir(out_path("bronze", "towers"))
    with open(OUT_DIR / "towers.geojsonl", "w") as fh:
        for r in t.itertuples(index=False):
            fh.write(feature(
                {"type": "Point", "coordinates": [round(r.lon, 5),
                                                  round(r.lat, 5)]},
                {"asr": r.asr_id,
                 "h": round(r.height_agl_m) if pd.notna(r.height_agl_m) else None,
                 "type": r.structure_type or None}) + "\n")
    return len(t)


def site_layer() -> int:
    from pyproj import Transformer

    s = read_dir(out_path("gold", "siting"))
    s = s[s["selected_greedy"]].sort_values("greedy_rank")
    to_4326 = Transformer.from_crs(5070, 4326, always_xy=True)
    with open(OUT_DIR / "sites.geojsonl", "w") as fh:
        for r in s.itertuples(index=False):
            lng, lat = to_4326.transform(r.x, r.y)
            fh.write(feature(
                {"type": "Point", "coordinates": [round(lng, 5), round(lat, 5)]},
                {"rank": int(r.greedy_rank),
                 "kind": r.kind,
                 "h": round(r.tx_height_m),
                 "gain": round(r.marginal_demand)}) + "\n")
    return len(s)


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    n_hex, n_tow, n_site = hex_layer(), tower_layer(), site_layer()
    meta = {"threshold_dbm": RF["rsrp_threshold_dbm"],
            "frequency_mhz": RF["frequency_mhz"]}
    (OUT_DIR / "meta.json").write_text(json.dumps(meta))
    print(f"wrote {n_hex} hexes, {n_tow} towers, {n_site} sites -> {OUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
