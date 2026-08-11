"""Per-transmitter propagation footprints for the map's click behaviour.

The hex map answers "how good is signal HERE". This answers the other half:
"how far does THAT transmitter actually reach, and what stops it" -- click one
structure and see its own coverage, cell by cell, with the loss budget that
produced each cell.

Nothing new is modelled here. `silver/links` already holds every
transmitter/cell pair the pipeline evaluated, with the budget decomposed
(free-space + diffraction + clutter, plus geometric line of sight). This
script is a REPACKAGING of that table into a form a static web page can query
one transmitter at a time. The one exception is the optimizer's recommended
sites: they are not registered structures, so no link row exists for them and
their footprints are computed here with the same kernel (`link_rsrp`, pure
numpy -- 20 transmitters against every cell in range is seconds, no Spark).

## Why a binary blob and a byte index, not one file per transmitter

3,126 transmitters would be 3,126 files. One 14 MB blob with a JSON index of
byte offsets is one file, and the client fetches only the slice it needs with
an HTTP Range request -- median 548 records, about 6.5 KB. GitHub Pages
already serves range requests for `rf.pmtiles`, so this rides on serving
behaviour the project depends on anyway.

Record layout, little-endian, 12 bytes, no padding:

    u64  h3      H3 r8 cell index (the client turns it back into a hexagon)
    i8   rsrp    predicted RSRP from THIS transmitter, dBm, rounded
    u8   diff    diffraction loss, dB
    u8   clut    clutter loss (land-cover class or building knife edge), dB
    u8   los     1 = geometric line of sight, 0 = terrain blocks the ray

FLOOR_DBM truncates the tail. A cell 10 dB below the coverage threshold
contributes nothing a reader can act on, and keeping it would nearly double
the file: 2.29M links above -125 dBm against 1.20M above -115. The cut is
stated in the index and on the page, because a footprint that stops at a
contour must not read as the edge of the physics.

Usage (statewide inputs pulled from S3 into data/state):
    RFC_DATA_DIR=data/state SCOPE=state LOCAL_OUT=1 \
        python scripts/make_footprints.py
"""
import glob
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from config import BUILDINGS, REPO_ROOT, RF  # noqa: E402
from session import out_path  # noqa: E402

OUT_DIR = REPO_ROOT / "web" / "data"

# 10 dB below the coverage threshold. See the module docstring.
FLOOR_DBM = -115.0

# numpy packs a structured dtype without padding, so this IS the wire format
# and `.tobytes()` is the whole writer.
REC = np.dtype([("h3", "<u8"), ("rsrp", "i1"), ("diff", "u1"),
                ("clut", "u1"), ("los", "u1")])


def pack(df: pd.DataFrame) -> bytes:
    """One transmitter's rows -> its slice of the blob.

    Values are rounded to whole dB and clipped to the byte ranges. Clipping
    rather than wrapping matters: a 300 dB diffraction loss on a hopeless path
    is real, and `u1` arithmetic would silently turn it into 44.
    """
    rec = np.empty(len(df), dtype=REC)
    rec["h3"] = df["h3_r8"].to_numpy(dtype="uint64")
    rec["rsrp"] = np.clip(np.rint(df["rsrp_dbm"]), -128, 127)
    rec["diff"] = np.clip(np.rint(df["diffraction_db"]), 0, 255)
    rec["clut"] = np.clip(np.rint(df["clutter_db"]), 0, 255)
    rec["los"] = df["is_los"].to_numpy(dtype="uint8")
    return rec.tobytes()


def unpack(blob: bytes, offset: int = 0, count: int | None = None):
    """The client's decode, in numpy, so the round trip can be asserted."""
    n = len(blob) // REC.itemsize if count is None else count
    return np.frombuffer(blob, dtype=REC, count=n, offset=offset)


def self_check() -> None:
    """Round-trip the encoder on values chosen to break it.

    Runs on every build AND offline in `make test`: the packing is the only
    part of this script that can be wrong in a way no eyeball catches, since a
    one-byte slip shows up as a plausible-looking map of the wrong place.
    """
    df = pd.DataFrame({
        # A real r8 index, and the largest one H3 can produce, so a signed
        # 64-bit read anywhere in the chain fails here rather than in a browser.
        "h3_r8": [613237248420216831, 0x8FFFFFFFFFFFFFF, 613237241560432639],
        "rsrp_dbm": [-94.5, -104.5, -115.0],
        "diffraction_db": [0.0, 7.72, 400.0],      # 400 must clip, not wrap
        "clutter_db": [12.0, 30.0, 0.4],
        "is_left_out": [1, 2, 3],
        "is_los": [True, False, True],
    })
    got = unpack(pack(df))
    assert got["h3"].tolist() == df["h3_r8"].tolist(), got["h3"]
    # numpy rounds halves to even: -94.5 -> -94, -104.5 -> -104. Asserted
    # rather than assumed, because the alternative (-95, -105) would move a
    # cell across the coverage threshold in the legend.
    assert got["rsrp"].tolist() == [-94, -104, -115], got["rsrp"].tolist()
    assert got["diff"].tolist() == [0, 8, 255], got["diff"].tolist()
    assert got["clut"].tolist() == [12, 30, 0], got["clut"].tolist()
    assert got["los"].tolist() == [1, 0, 1], got["los"].tolist()
    assert len(pack(df)) == 3 * 12, "record must be 12 bytes with no padding"
    print("footprint pack/unpack self-check: 6 assertions passed")


def read_dir(path: str, **kw) -> pd.DataFrame:
    files = sorted(glob.glob(f"{path}/*.parquet"))
    if not files:
        raise FileNotFoundError(f"no parquet under {path}")
    return pd.concat((pd.read_parquet(f, **kw) for f in files),
                     ignore_index=True)


def site_links(cov: pd.DataFrame) -> pd.DataFrame:
    """Footprints for the recommended sites, which have no link rows.

    Same kernel and the same terrain the pipeline used, called directly:
    `link_rsrp` is pure numpy, and 20 transmitters against the cells within
    max_link_km is a few hundred thousand pairs -- a Spark job's startup cost
    exceeds the work.
    """
    import importlib

    links05 = importlib.import_module("05_links")
    from propagation import link_rsrp

    sit = read_dir(out_path("gold", "siting"))
    sit = sit[sit["selected_greedy"]].sort_values("greedy_rank")
    grid = links05.load_terrain(
        out_path("cog", "dem_5070_90m.tif"),
        out_path("cog", "clutter_5070_90m.tif"),
        out_path("cog", "buildings_5070_90m.tif"),
    )
    radius_m = float(RF["max_link_km"]) * 1000.0
    cx, cy = cov["x"].to_numpy(), cov["y"].to_numpy()

    out = []
    for r in sit.itertuples(index=False):
        near = np.hypot(cx - r.x, cy - r.y) <= radius_m
        n = int(near.sum())
        if not n:
            continue
        res = link_rsrp(
            grid,
            tx_x=np.full(n, r.x), tx_y=np.full(n, r.y),
            tx_height_m=np.full(n, r.tx_height_m),
            rx_x=cx[near], rx_y=cy[near],
            rx_height_m=RF["rx_height_m"], freq_mhz=RF["frequency_mhz"],
            eirp_dbm=RF["eirp_dbm"], subcarriers=RF["subcarriers"],
            shadow_margin_db=RF["shadow_margin_db"],
            n_samples=RF["profile_samples"], k_factor=RF["k_factor"],
            bldg_setback_m=float(BUILDINGS["setback_m"]),
            bldg_max_loss_db=float(BUILDINGS["max_loss_db"]),
        )
        out.append(pd.DataFrame({
            "asr_id": f"NEW:{int(r.greedy_rank)}",
            "h3_r8": cov["h3_r8"].to_numpy()[near],
            "rsrp_dbm": res["rsrp_dbm"],
            "diffraction_db": res["diffraction_db"],
            "clutter_db": res["clutter_db"],
            "is_los": res["is_los"],
        }))
    df = pd.concat(out, ignore_index=True)
    print(f"recommended sites: {len(sit)} transmitters, {len(df):,} pairs "
          f"evaluated with the same kernel")
    return df


def main() -> int:
    self_check()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    threshold = float(RF["rsrp_threshold_dbm"])

    cov = read_dir(out_path("gold", "coverage"),
                   columns=["h3_r8", "x", "y", "pop"])
    links = read_dir(out_path("silver", "links"),
                     columns=["asr_id", "h3_r8", "rsrp_dbm", "diffraction_db",
                              "clutter_db", "is_los"])
    print(f"{len(links):,} registered-structure links, {len(cov):,} cells")

    df = pd.concat([links, site_links(cov)], ignore_index=True)
    df = df[df["rsrp_dbm"] >= FLOOR_DBM]

    # Population per cell, so the index can carry what each transmitter is
    # actually worth in people rather than only in hexagons.
    pop = cov.set_index("h3_r8")["pop"]
    df["pop"] = df["h3_r8"].map(pop).fillna(0.0)

    # Sorted by transmitter so each one's records are contiguous, which is
    # what makes a single Range request possible. The secondary sort is by
    # signal, so a truncated read would still hold the strongest cells.
    df = df.sort_values(["asr_id", "rsrp_dbm"], ascending=[True, False])

    blob, index, offset = [], {}, 0
    for asr, g in df.groupby("asr_id", sort=True):
        chunk = pack(g)
        blob.append(chunk)
        served = g["rsrp_dbm"] >= threshold
        index[str(asr)] = [offset, len(g), int(served.sum()),
                           int(round(g.loc[served, "pop"].sum()))]
        offset += len(chunk)

    data = b"".join(blob)
    (OUT_DIR / "footprints.bin").write_bytes(data)
    (OUT_DIR / "footprints.json").write_text(json.dumps({
        "floor_dbm": FLOOR_DBM,
        "threshold_dbm": threshold,
        "record_bytes": REC.itemsize,
        "bytes": len(data),
        "tx": index,
    }, separators=(",", ":")))

    # Read back what was written, not what is in memory: this is the only
    # check that the offsets in the index actually address the rows the index
    # claims. An off-by-one-record here paints one transmitter's coverage
    # under another transmitter's name, which looks entirely plausible.
    on_disk = (OUT_DIR / "footprints.bin").read_bytes()
    for asr in list(index)[::311] + [df["asr_id"].iloc[-1]]:
        off, n, _, _ = index[asr]
        got = unpack(on_disk, off, n)
        want = df[df["asr_id"] == asr]
        assert got["h3"].tolist() == want["h3_r8"].tolist(), \
            f"{asr}: index offset does not address its own records"
        assert got["rsrp"].max() == round(want["rsrp_dbm"].max()), asr

    n_cells = df.groupby("asr_id").size()
    print(f"wrote {len(index):,} transmitter footprints, {len(df):,} records, "
          f"{len(data) / 1e6:.1f} MB -> {OUT_DIR / 'footprints.bin'}")
    print(f"  cells per transmitter: median {int(n_cells.median())}, "
          f"p95 {int(n_cells.quantile(0.95))}, max {int(n_cells.max())} "
          f"({int(n_cells.max()) * REC.itemsize / 1024:.0f} KB worst-case fetch)")
    print(f"  truncated at {FLOOR_DBM:.0f} dBm "
          f"({threshold - FLOOR_DBM:.0f} dB below the coverage threshold)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
