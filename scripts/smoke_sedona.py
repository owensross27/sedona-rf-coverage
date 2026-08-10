"""Smallest check that the whole local stack resolves: JDK -> pyspark ->
Sedona jars -> ST_* functions -> the specific spatial join this project is
built on. Run with `make smoke`.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
os.environ.setdefault("LOCAL_MASTER", "local[2]")

from session import assert_versions, get_sedona  # noqa: E402


def main() -> int:
    sedona = get_sedona("rfc-smoke")
    assert_versions(sedona)
    sedona.sparkContext.setLogLevel("ERROR")

    wkt = sedona.sql("SELECT ST_AsText(ST_Point(1.0, 2.0)) AS p").collect()[0]["p"]
    assert wkt == "POINT (1 2)", wkt

    # The join 05_links depends on: towers to receivers inside the link radius.
    # One pair is inside 40 km and three are outside, so the predicate is
    # actually exercised rather than trivially true.
    n = sedona.sql("""
        SELECT count(*) AS n FROM
          (SELECT ST_Point(0.0, 0.0) AS g UNION ALL
           SELECT ST_Point(300000.0, 0.0)) t,
          (SELECT ST_Point(1000.0, 0.0) AS c UNION ALL
           SELECT ST_Point(500000.0, 0.0)) h
        WHERE ST_DWithin(t.g, h.c, 40000.0)
    """).collect()[0]["n"]
    assert n == 1, f"ST_DWithin matched {n} pairs, expected 1"

    # H3 is how receivers are keyed; confirm the binding exists before 04_grid
    # relies on it.
    cell = sedona.sql(
        "SELECT ST_H3CellIDs(ST_Point(-81.6, 38.3), 8, false) AS c"
    ).collect()[0]["c"]
    assert cell, "ST_H3CellIDs returned nothing"

    print(f"smoke ok: sedona {getattr(__import__('sedona'), '__version__', '?')}, "
          f"ST_DWithin + ST_H3CellIDs resolve")
    sedona.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
