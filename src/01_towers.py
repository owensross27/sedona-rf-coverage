"""Stage 01 -- FCC Antenna Structure Registration -> bronze/towers GeoParquet.

The transmitter inventory. This is the weakest input in the project and the
one worth being loudest about:

  ASR structures are not cell sites. Registration is required for structures
  over 200 ft or near an airport, so the file contains AM/FM broadcast masts
  and water towers, and misses every rooftop and small-cell installation in
  the state. Modelling coverage from it therefore over-counts tall rural
  structures and under-counts urban sites. That bias is not fixable from open
  data, so it is not fixed here -- it is quantified in stage S34 by
  reconciling this table against FCC BDC coverage polygons.

Two file-format traps, both verified against the 2026-08-09 dump:

  1. Coordinates are separate degree / minute / second columns. There is also
     a redundant total-seconds column, and the two are cross-checked here
     rather than one being trusted -- a sign or unit error in this conversion
     would place towers in the wrong hemisphere and everything downstream
     would still run.
  2. RA.dat carries four height columns and they are easy to confuse. The one
     an antenna actually sits at is `overall_height_above_ground`, not
     `height_of_structure`. The arithmetic identity ground + overall_agl ==
     overall_amsl is asserted below to prove the columns were read in the
     right order.

Usage:
    SCOPE=demo LOCAL_OUT=1 python src/01_towers.py
"""
import sys
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import SOURCES  # noqa: E402
from session import get_sedona, out_path  # noqa: E402
from sources import RAW_DIR, aoi, cached, grid_spec, link_pad_m  # noqa: E402

# Pipe-delimited, no header. Field positions verified against the 2026-08-09
# dump; the ASR layout is not the standard ULS layout (position 1 is a "REG"
# content indicator, not the system identifier), so these are not guesses
# carried over from another FCC dataset.
CO = {"usi": 3, "kind": 5,
      "lat_d": 6, "lat_m": 7, "lat_s": 8, "lat_dir": 9, "lat_total_s": 10,
      "lon_d": 11, "lon_m": 12, "lon_s": 13, "lon_dir": 14, "lon_total_s": 15}
RA = {"usi": 3, "asr_id": 4, "status": 8, "city": 24, "state": 25,
      "county_fips": 26, "height_struct": 28, "ground_elev": 29,
      "height_agl": 30, "height_amsl": 31, "structure_type": 32}

# ASR status codes. 'C' is Constructed. 'G' (granted, not yet built) and 'A'
# are deliberately excluded: a structure that does not exist does not radiate,
# and including them would inflate coverage with towers that are not there.
BUILT = "C"

# Structure-level coordinate records. 'A' rows are appurtenance positions on
# the same structure and would duplicate every tower.
COORD_STRUCTURE = "T"


def _rows(path: Path, want: dict):
    """Yield the wanted fields from a pipe-delimited ASR file.

    Hand-split rather than pandas.read_csv because free-text fields in this
    dump (owner names, FAA remarks) contain unescaped pipes, so the row width
    is not constant and a fixed `names=` list drops or shifts rows silently.
    Positional access to a known prefix of the row is immune to that.
    """
    hi = max(want.values())
    with open(path, encoding="latin-1") as fh:
        for line in fh:
            r = line.rstrip("\n").split("|")
            if len(r) > hi:
                yield {k: r[i] for k, i in want.items()}


def _dms_to_deg(d, m, s, direction, negative: str) -> np.ndarray:
    deg = d + m / 60.0 + s / 3600.0
    return np.where(direction == negative, -deg, deg)


def _num(series: pd.Series) -> pd.Series:
    """ASR blanks are empty strings, not nulls."""
    return pd.to_numeric(series.str.strip().replace("", None), errors="coerce")


def load_asr() -> pd.DataFrame:
    """Parse the ASR archive into one row per constructed structure."""
    zip_path = cached(SOURCES["asr"]["url"], "r_tower.zip")
    extract = RAW_DIR / "asr"
    if not (extract / "RA.dat").exists():
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(extract)

    co = pd.DataFrame(_rows(extract / "CO.dat", CO))
    co = co[co["kind"] == COORD_STRUCTURE]

    ra = pd.DataFrame(_rows(extract / "RA.dat", RA))
    ra = ra[ra["status"] == BUILT]

    df = ra.merge(co.drop(columns="kind"), on="usi", how="inner", validate="1:1")

    for col in ("lat_d", "lat_m", "lat_s", "lat_total_s",
                "lon_d", "lon_m", "lon_s", "lon_total_s",
                "height_struct", "ground_elev", "height_agl", "height_amsl"):
        df[col] = _num(df[col])

    df["lat"] = _dms_to_deg(df["lat_d"], df["lat_m"], df["lat_s"],
                            df["lat_dir"], "S")
    df["lon"] = _dms_to_deg(df["lon_d"], df["lon_m"], df["lon_s"],
                            df["lon_dir"], "W")

    # Trap 1: the redundant total-seconds column must reproduce the D/M/S
    # assembly. A tenth of an arcsecond is ~3 m, well under the 90 m grid.
    for axis, total, deg in (("lat", "lat_total_s", "lat"),
                             ("lon", "lon_total_s", "lon")):
        drift = (df[total] / 3600.0 - df[deg].abs()).abs()
        bad = drift > (0.1 / 3600.0)
        if bad.any():
            raise RuntimeError(
                f"{bad.sum()} rows disagree between {axis} D/M/S and the "
                f"total-seconds column (max {drift.max() * 3600:.2f} arcsec); "
                "the ASR field layout in this file has changed"
            )

    # Trap 2: prove the four height columns were read in the right order.
    h = df.dropna(subset=["ground_elev", "height_agl", "height_amsl"])
    resid = (h["ground_elev"] + h["height_agl"] - h["height_amsl"]).abs()
    if len(h) and resid.median() > 1.0:
        raise RuntimeError(
            "ground_elevation + overall_height_agl != overall_height_amsl "
            f"(median residual {resid.median():.1f} m); RA.dat height columns "
            "are not where this parser expects them"
        )

    df = df.dropna(subset=["lat", "lon"])
    # Heights of zero are "not reported", not a ground-level antenna.
    df.loc[df["height_agl"] <= 0, "height_agl"] = np.nan
    return df


def main() -> int:
    df = load_asr()
    print(f"ASR constructed structures with coordinates: {len(df)}")

    # Project once, on the driver, with pyproj -- 200k points is not a Spark
    # problem and doing it here keeps the bbox filter below trivial.
    from pyproj import Transformer

    from config import GRID
    tf = Transformer.from_crs(4326, GRID["crs"], always_xy=True)
    df["x"], df["y"] = tf.transform(df["lon"].to_numpy(), df["lat"].to_numpy())

    # Transmitters come from the PADDED extent, not the scope polygon: a tower
    # outside the county line still serves receivers inside it, and clipping
    # them would draw a fake uncovered ring around the whole boundary.
    #
    # The padded *bounding box* is used rather than the padded polygon on
    # purpose. Anything inside the box but outside the polygon is further than
    # max_link_km from every receiver, so ST_DWithin in 05_links.py drops it
    # anyway, and a box test is a comparison instead of a point-in-polygon
    # against a 55-county multipolygon.
    minx, miny, maxx, maxy = grid_spec(aoi(link_pad_m()))["bounds"]
    inside = df["x"].between(minx, maxx) & df["y"].between(miny, maxy)
    df = df[inside]
    print(f"within padded scope extent: {len(df)}")
    if df.empty:
        raise RuntimeError("no towers in scope; check SCOPE and county FIPS")

    keep = df[["asr_id", "usi", "county_fips", "structure_type", "x", "y",
               "lat", "lon", "height_agl", "height_struct", "ground_elev"]]
    keep = keep.rename(columns={"height_agl": "height_agl_m",
                                "height_struct": "height_struct_m",
                                "ground_elev": "ground_elev_m"})
    keep = keep.assign(structure_type=keep["structure_type"].str.strip())

    sedona = get_sedona("rf-towers")
    sdf = sedona.createDataFrame(keep)
    sdf.createOrReplaceTempView("t")
    # ST_Point builds the GeometryUDT that 05_links.py's ST_DWithin join needs.
    # The x/y columns are kept alongside it: the kernel wants plain doubles and
    # re-extracting them with ST_X downstream would be a round trip for nothing.
    geo = sedona.sql("SELECT *, ST_Point(x, y) AS geom FROM t")

    dest = out_path("bronze", "towers")
    geo.write.format("geoparquet").mode("overwrite").save(dest)
    print(f"wrote {geo.count()} towers -> {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
