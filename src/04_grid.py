"""Stage 04 -- H3 r8 receiver grid with a demand score -> silver/hex_grid.

The demand side of the model. Every hexagon that 05_links.py will try to serve
is created here, together with the number that decides whether serving it is
worth a tower:

    demand = pop * (1 + county_growth) + tourism_weight * sum(poi weights)

Tourism is ADDITIVE rather than multiplicative because a trailhead has close to
zero residents and real demand; multiplying would zero it out precisely where
the interesting siting answers are.

Population reaches the hexes by AREAL INTERPOLATION, not by point-sampling the
block group under each hex centre. Block groups in Charleston are smaller than
an r8 hexagon while rural ones are many times larger, so a point sample would
drop whole urban block groups and duplicate rural ones. Interpolation by area
fraction conserves the population total, which is a property this stage
asserts rather than assumes.

Usage:
    SCOPE=demo LOCAL_OUT=1 python src/04_grid.py
"""
import sys
from pathlib import Path

import geopandas as gpd
import h3
import numpy as np
import pandas as pd
import shapely

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import DEMAND, GRID, SOURCES  # noqa: E402
from session import get_sedona, out_path  # noqa: E402
from sources import aoi, block_groups  # noqa: E402

RES = int(GRID["receiver_res"])


def receiver_cells(aoi_4326) -> gpd.GeoDataFrame:
    """Every H3 r8 cell whose centre falls inside the scope polygon.

    h3.geo_to_cells takes GeoJSON axis order (lng, lat) and uses centre
    containment, so cells tile the polygon without overlap or double counting
    along the boundary.
    """
    cells = sorted(h3.geo_to_cells(aoi_4326, RES))
    if not cells:
        raise RuntimeError("scope polygon produced no H3 cells")
    lat, lng = np.array([h3.cell_to_latlng(c) for c in cells]).T
    poly = [shapely.Polygon([(x, y) for y, x in h3.cell_to_boundary(c)])
            for c in cells]
    gdf = gpd.GeoDataFrame(
        {"h3_str": cells,
         "h3_r8": [h3.str_to_int(c) for c in cells],
         "lat": lat, "lng": lng},
        geometry=poly, crs=4326,
    )
    return gdf.to_crs(GRID["crs"])


def interpolate_population(hexes: gpd.GeoDataFrame, bg: gpd.GeoDataFrame
                           ) -> pd.DataFrame:
    """Areal interpolation of block-group population onto hexagons.

    Growth is applied here, at the intersection level, rather than after
    aggregation. A hexagon on a county line genuinely draws from two counties
    with two different growth rates, and weighting each piece by the population
    it contributes is both more correct and less code than picking a winner.
    """
    bg = bg.copy()
    bg["bg_area"] = bg.geometry.area
    parts = gpd.overlay(
        hexes[["h3_r8", "geometry"]],
        bg[["pop", "growth", "bg_area", "geometry"]],
        how="intersection", keep_geom_type=True,
    )
    frac = parts.geometry.area / parts["bg_area"]
    parts["pop_share"] = parts["pop"] * frac
    parts["grown_share"] = parts["pop_share"] * (1.0 + parts["growth"].fillna(0.0))
    return parts.groupby("h3_r8")[["pop_share", "grown_share"]].sum()


def tourism_score(state_bounds_4326) -> pd.DataFrame:
    """Overture places -> a weighted tourism score per H3 cell.

    Queried over the whole STATE, never the scope polygon, for two reasons.
    A place just outside a county still spreads demand into it through the
    k-ring, and -- more importantly -- the category-vocabulary check below is
    only meaningful statewide. Kanawha County has no ski resort, so at demo
    scope a per-scope check would fire on a correct config. Cells outside the
    scope are dropped by the join in main(); the scan cost is the same either
    way because row-group pruning already touches these files.

    Read with duckdb rather than Spark: the `bbox` struct column lets the
    parquet reader prune row groups by extent, so a state-sized window touches
    a few hundred MB of a ~10 GB global theme. That pruning is the whole reason
    this is cheap, and it is a Spark-side capability this project simply does
    not need to reimplement.

    The full weight is applied to every cell within poi_k_ring of the place,
    not divided among them. Demand is a need for coverage, not a conserved
    quantity -- a ski resort needs signal in its car park and on its access
    road as much as at the lodge -- and config.yml calibrates tourism_weight
    against that reading ("a major ski resort is worth roughly 5000 effective
    residents" is 5.0 x 1000 on each of its cells).
    """
    import duckdb

    weights = {str(k): float(v) for k, v in DEMAND["poi_weights"].items()}
    ov = SOURCES["overture"]
    path = (f"s3://{ov['bucket']}/release/{ov['release']}"
            "/theme=places/type=place/*")
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs; SET s3_region='us-west-2';")
    minx, miny, maxx, maxy = state_bounds_4326
    # Coordinates come from the `bbox` struct rather than the geometry column
    # so the duckdb spatial extension is never needed. Places are points, so
    # their bbox is degenerate (xmin == xmax) and this is exact, not an
    # approximation -- and it keeps a 10 MB extension download off the critical
    # path of every executor.
    df = con.execute(
        f"""
        SELECT categories.primary AS cat,
               bbox.xmin AS lng, bbox.ymin AS lat
        FROM read_parquet('{path}')
        WHERE bbox.xmin BETWEEN ? AND ? AND bbox.ymin BETWEEN ? AND ?
          AND categories.primary IN ({','.join('?' * len(weights))})
        """,
        [minx, maxx, miny, maxy, *weights.keys()],
    ).df()

    found = df["cat"].value_counts().to_dict()
    print(f"Overture POIs in scope: {len(df)}")
    for cat in weights:
        print(f"  {cat}: {found.get(cat, 0)}")
    # A category in config.yml that matches nothing is a silent zero in the
    # demand score, not an error anywhere. Overture's vocabulary is theirs to
    # change, so the mismatch is caught here instead of showing up as a
    # tourism term that is quietly smaller than intended.
    empty = [c for c in weights if not found.get(c)]
    if empty:
        raise RuntimeError(
            f"POI categories matched zero Overture places: {empty}. Check them "
            "against `SELECT DISTINCT categories.primary` for this release "
            "before assuming the region simply has none."
        )

    k = int(DEMAND["poi_k_ring"])
    score: dict[int, float] = {}
    for cat, lat, lng in df[["cat", "lat", "lng"]].itertuples(index=False):
        w = weights[cat]
        for cell in h3.grid_disk(h3.latlng_to_cell(lat, lng, RES), k):
            idx = h3.str_to_int(cell)
            score[idx] = score.get(idx, 0.0) + w
    return pd.DataFrame({"h3_r8": list(score), "poi_score": list(score.values())})


def main() -> int:
    geom = aoi()
    aoi_4326 = gpd.GeoSeries([geom], crs=GRID["crs"]).to_crs(4326).iloc[0]

    hexes = receiver_cells(aoi_4326)
    print(f"receiver cells at H3 r{RES}: {len(hexes)}")

    sedona = get_sedona("rf-grid")
    bg_sdf = sedona.read.format("geoparquet").load(out_path("bronze", "blockgroups"))
    bg_pdf = bg_sdf.selectExpr("pop", "growth", "ST_AsBinary(geom) AS wkb").toPandas()
    bg = gpd.GeoDataFrame(
        bg_pdf.drop(columns="wkb"),
        geometry=gpd.GeoSeries.from_wkb(bg_pdf["wkb"]), crs=GRID["crs"],
    )

    pop = interpolate_population(hexes, bg)
    hexes = hexes.join(pop, on="h3_r8")
    hexes[["pop_share", "grown_share"]] = hexes[["pop_share", "grown_share"]].fillna(0.0)

    # Areal interpolation conserves population. Anything that escaped is
    # population in a block group that no hexagon covers, which means the H3
    # tiling and the block-group extent disagree -- exactly the silent bug this
    # stage exists to avoid.
    got, want = hexes["pop_share"].sum(), bg["pop"].sum()
    print(f"population: {got:,.0f} interpolated of {want:,.0f} in block groups "
          f"({got / want:.2%})")
    if want > 0 and abs(got - want) / want > 0.02:
        raise RuntimeError(
            f"areal interpolation lost {1 - got / want:.1%} of the population; "
            "the H3 cover and the block-group polygons do not agree"
        )

    # State bounds come from the full TIGER file (already cached, and read
    # again here at no cost) rather than config.yml's bounds_5070, so the POI
    # extent cannot drift away from the geography the rest of the stage uses.
    state_4326 = tuple(block_groups().to_crs(4326).total_bounds)
    poi = tourism_score(state_4326)
    hexes = hexes.merge(poi, on="h3_r8", how="left")
    hexes["poi_score"] = hexes["poi_score"].fillna(0.0)

    hexes["demand"] = (hexes["grown_share"]
                       + float(DEMAND["tourism_weight"]) * hexes["poi_score"])
    print(f"demand: total {hexes['demand'].sum():,.0f}, "
          f"{(hexes['poi_score'] > 0).sum()} cells carry a tourism term")

    # The receiver point is the cell centre, in the same EPSG:5070 metres the
    # terrain grid and the towers use, so 05_links.py's ST_DWithin predicate is
    # a plain Euclidean distance.
    out = pd.DataFrame({
        "h3_r8": hexes["h3_r8"].astype("int64"),
        "h3_str": hexes["h3_str"],
        "pop": hexes["pop_share"].astype(float),
        "pop_grown": hexes["grown_share"].astype(float),
        "poi_score": hexes["poi_score"].astype(float),
        "demand": hexes["demand"].astype(float),
        "x": hexes.geometry.centroid.x.astype(float),
        "y": hexes.geometry.centroid.y.astype(float),
    })
    sedona.createDataFrame(out).createOrReplaceTempView("h")
    geo = sedona.sql("SELECT *, ST_Point(x, y) AS center FROM h")

    dest = out_path("silver", "hex_grid")
    geo.write.format("geoparquet").mode("overwrite").save(dest)
    print(f"wrote {geo.count()} receiver cells -> {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
