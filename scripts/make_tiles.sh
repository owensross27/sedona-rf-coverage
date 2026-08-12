#!/usr/bin/env bash
# Gold outputs -> web/data/rf.pmtiles.
#
# PMTiles because the serving story must survive cluster teardown: one static
# file, HTTP range requests, no tile server anywhere. Locally it is served by
# `make web-serve`; in production the same file sits behind GitHub Pages or
# CloudFront.
#
# SIX layers on FOUR DISJOINT ZOOM BANDS. Only one hex layer is ever visible,
# so the number of features in view stays roughly constant while panning
# instead of growing sevenfold with every zoom-out.
#
#   z4-z5    hex5     H3 r5, ~320 cells statewide.  253 km2, ~20 km across
#   z6-z8    hex6     H3 r6, ~1,950                  36 km2, ~7.5 km across
#   z9-z10   hex7     H3 r7, ~12,900                 5.2 km2, ~2.8 km across
#   z11+     hexes    H3 r8, 88,281 -- full detail   0.74 km2, ~1.1 km across
#
# BANDS TIGHTENED 2026-08-11. r5 originally held z4-z8, i.e. every zoom at
# which West Virginia fits on a screen -- so the statewide view, the one people
# actually look at, was drawn in 20 km hexagons that smeared whole counties
# into a single colour. r6 now owns that range: 6x more cells, still only
# ~1,950, and each level hands over two zooms earlier than before. r8 arrives at
# z11 rather than z13, which is the earliest it fits: a z11 tile spans ~19.6 km
# and holds roughly 450 r8 cells, comfortably inside tippecanoe's size limit,
# while z10 would put ~1,800 in one tile and start pushing against it.
#
# WHY NOT --drop-densest-as-needed, which the demo-scope version used: it keeps
# low-zoom tiles small by THROWING FEATURES AWAY. On a choropleth that leaves
# holes, and a viewer cannot tell a dropped cell from an uncovered one -- the
# map gets faster by lying about the thing it exists to show. H3's own
# hierarchy does the same job honestly: cell_to_parent is a bit shift, every
# cell has exactly one parent, and the roll-up is asserted to conserve both
# cell count and population.
#
# tippecanoe cannot give two layers different zoom ranges in one invocation,
# so each band is built separately and tile-join merges them into one archive.
set -euo pipefail
cd "$(dirname "$0")/.."

SCOPE="${SCOPE:-demo}" LOCAL_OUT="${LOCAL_OUT:-1}" .venv/bin/python scripts/make_web_data.py

D=web/data
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# -pC/--no-tile-compression: PMTiles range requests are read straight from the
# browser without a decompressing server in front of them.
TC=(tippecanoe --force --no-tile-compression)

# hex5 starts at z4, not at the z6 where West Virginia first fits a screen.
# MapLibre requests NOTHING below a source's minzoom, so a client that opens
# with fitBounds on a tall window lands at z5.3 and renders a completely blank
# map with no error anywhere -- measured, not hypothesised. z4 costs a handful
# of tiles (320 r5 cells land in one or two) and removes the whole class.

"${TC[@]}" -o "$TMP/hex5.pmtiles"  -Z4  -z5  -l hex5  "$D/hex5.geojsonl"
"${TC[@]}" -o "$TMP/hex6.pmtiles"  -Z6  -z8  -l hex6  "$D/hex6.geojsonl"
"${TC[@]}" -o "$TMP/hex7.pmtiles"  -Z9  -z10 -l hex7  "$D/hex7.geojsonl"
"${TC[@]}" -o "$TMP/hex8.pmtiles"  -Z11 -z13 -l hexes "$D/hexes.geojsonl"
# Points are cheap and wanted at every zoom. -r1 keeps every tower rather than
# thinning by density: a missing tower reads as "no tower there", which is the
# same class of lie as a dropped hex.
"${TC[@]}" -o "$TMP/points.pmtiles" -Z4 -z13 -r1 \
  -L towers:"$D/towers.geojsonl" \
  -L sites:"$D/sites.geojsonl"

tile-join --force -o "$D/rf.pmtiles.png" \
  "$TMP/hex5.pmtiles" "$TMP/hex6.pmtiles" "$TMP/hex7.pmtiles" \
  "$TMP/hex8.pmtiles" "$TMP/points.pmtiles"

rm -f "$D"/hex5.geojsonl "$D"/hex6.geojsonl "$D"/hex7.geojsonl \
      "$D"/hexes.geojsonl "$D"/towers.geojsonl "$D"/sites.geojsonl

# GitHub Pages refuses files over 100 MB, so the size is a hard gate rather
# than a statistic. If this trips: drop the r8 max zoom, simplify geometry, or
# split into several PMTiles files (the limit is per file).
BYTES=$(wc -c < "$D/rf.pmtiles.png")
printf '== rf.pmtiles.png %.1f MB (GitHub Pages limit is 100 MB per file)\n' \
  "$(echo "$BYTES" | awk '{print $1/1048576}')"
[ "$BYTES" -lt 104857600 ] || { echo "ERROR: over the 100 MB Pages limit"; exit 1; }
