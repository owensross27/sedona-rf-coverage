"""Stage 03 -- TIGER block groups + ACS population -> bronze/blockgroups.

Where the demand side of the model gets its residents. Two outputs in one
table: block-group population (the quantity), and county-level growth (the
trend that decides whether a gap is worth closing).

Growth is county-level ON PURPOSE and this is not a simplification to be
tidied up later. Block-group boundaries were redrawn between the 2010 and 2020
censuses, so joining a 2019 block group to a 2024 one by GEOID compares two
different pieces of ground. It does not fail -- it returns a plausible growth
rate for the wrong geography, everywhere, silently. Counties are stable across
both vintages, so the trend is computed there and broadcast down.

There is no public GeoParquet of TIGER block groups anywhere on S3 or Source
Cooperative -- searched, not found. The shapefile-to-GeoParquet conversion here
is small, but it is the one piece of this pipeline that produces something
that did not previously exist in cloud-native form.

Requires a free Census API key exported as CENSUS_API_KEY. Keyless requests do
not error: they return HTTP 200 and an HTML "missing key" page, which parses
as neither JSON nor an exception. That is checked for explicitly below.

Usage:
    CENSUS_API_KEY=... SCOPE=demo LOCAL_OUT=1 python src/03_census.py
"""
import os
import sys
from pathlib import Path

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import DEMAND, SOURCES, STATE, scope  # noqa: E402
from session import get_sedona, out_path  # noqa: E402
from sources import block_groups  # noqa: E402

ACS_BASE = SOURCES["acs"]["base"]
POP = SOURCES["acs"]["variable"]


def _api_key() -> str:
    key = os.environ.get("CENSUS_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "CENSUS_API_KEY is unset. Get a free key at "
            "https://api.census.gov/data/key_signup.html -- note that an "
            "unkeyed request returns HTTP 200 and an HTML page rather than "
            "failing, so this is checked here instead of at the API."
        )
    return key


def _get(url: str, params: dict) -> list[list[str]]:
    """One ACS call, with the silent-HTML failure mode turned into an error."""
    r = requests.get(url, params=params, timeout=60)
    r.raise_for_status()
    ctype = r.headers.get("content-type", "")
    if "json" not in ctype:
        # The documented trap: 200 OK, a 302 to missing_key.html, and a body
        # that is a web page. Anything downstream would see a parse error a
        # long way from the cause.
        raise RuntimeError(
            f"ACS returned {ctype} rather than JSON from {r.url} "
            f"(final URL {r.url}). This is what a bad or missing "
            "CENSUS_API_KEY looks like."
        )
    return r.json()


def _frame(rows: list[list[str]]) -> pd.DataFrame:
    """ACS answers as [header, *rows]."""
    return pd.DataFrame(rows[1:], columns=rows[0])


def county_population(year: int, key: str) -> pd.DataFrame:
    rows = _get(f"{ACS_BASE}/{year}/acs/acs5", {
        "get": POP, "for": "county:*", "in": f"state:{STATE['fips']}", "key": key,
    })
    df = _frame(rows)
    return pd.DataFrame({
        "county_fips": df["state"] + df["county"],
        f"pop_{year}": pd.to_numeric(df[POP], errors="coerce"),
    })


def block_group_population(counties: list[str], key: str, year: int) -> pd.DataFrame:
    """ACS population by block group.

    Queried one county at a time because the ACS geography hierarchy requires
    a specific county for block-group requests -- `county:*` is not accepted at
    this level for every vintage, and looping is cheap next to being wrong.
    """
    out = []
    for fips in counties:
        rows = _get(f"{ACS_BASE}/{year}/acs/acs5", {
            "get": POP,
            "for": "block group:*",
            "in": f"state:{fips[:2]} county:{fips[2:]} tract:*",
            "key": key,
        })
        df = _frame(rows)
        out.append(pd.DataFrame({
            "GEOID": df["state"] + df["county"] + df["tract"] + df["block group"],
            "pop": pd.to_numeric(df[POP], errors="coerce"),
        }))
        print(f"  {fips}: {len(out[-1])} block groups")
    return pd.concat(out, ignore_index=True)


def main() -> int:
    key = _api_key()
    bg = block_groups()
    counties = scope()["counties"]
    if counties:
        bg = bg[bg["GEOID"].str[:5].isin(counties)].copy()
    else:
        counties = sorted(bg["GEOID"].str[:5].unique())
    print(f"scope: {len(counties)} counties, {len(bg)} block groups")

    old, new = DEMAND["acs_growth_vintages"]
    pop = block_group_population(counties, key, new)

    growth = county_population(old, key).merge(
        county_population(new, key), on="county_fips", how="inner")
    # A county that reported zero population in the base year would divide by
    # zero; none do in WV, but the guard costs one line and the alternative is
    # an inf that propagates into every demand score in that county.
    growth["growth"] = (
        (growth[f"pop_{new}"] - growth[f"pop_{old}"])
        / growth[f"pop_{old}"].where(growth[f"pop_{old}"] > 0)
    ).fillna(0.0)
    print(f"county growth {old}->{new}: median {growth['growth'].median():+.1%}, "
          f"range {growth['growth'].min():+.1%} to {growth['growth'].max():+.1%}")

    bg = bg.merge(pop, on="GEOID", how="left")
    bg["county_fips"] = bg["GEOID"].str[:5]
    bg = bg.merge(growth[["county_fips", "growth"]], on="county_fips", how="left")

    missing = int(bg["pop"].isna().sum())
    if missing:
        print(f"WARNING: {missing} block groups had no ACS population; "
              "treating as 0 residents")
        bg["pop"] = bg["pop"].fillna(0.0)

    keep = bg[["GEOID", "county_fips", "pop", "growth", "ALAND", "geometry"]]
    keep = keep.rename(columns={"GEOID": "geoid", "ALAND": "aland_m2"})
    print(f"population in scope: {keep['pop'].sum():,.0f}")

    # Hand geometry to Spark as WKB and rebuild it there. geopandas cannot
    # write to an s3a:// path without pulling in s3fs, and Spark already has
    # the per-bucket credential wiring this project needs.
    sedona = get_sedona("rf-census")
    pdf = keep.drop(columns="geometry").assign(
        wkb=keep.geometry.to_wkb(), pop=keep["pop"].astype(float))
    sedona.createDataFrame(pdf).createOrReplaceTempView("bg")
    geo = sedona.sql("SELECT * EXCEPT (wkb), ST_GeomFromWKB(wkb) AS geom FROM bg")

    dest = out_path("bronze", "blockgroups")
    geo.write.format("geoparquet").mode("overwrite").save(dest)
    print(f"wrote {geo.count()} block groups -> {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
