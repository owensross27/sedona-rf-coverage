#!/usr/bin/env bash
# Gold outputs -> web/data/rf.pmtiles (three layers: hexes, towers, sites).
#
# PMTiles because the serving story must survive cluster teardown: one static
# file, HTTP range requests, no tile server anywhere. Locally it is served by
# `make web-serve`; in production the same file sits behind CloudFront.
set -euo pipefail
cd "$(dirname "$0")/.."

SCOPE="${SCOPE:-demo}" LOCAL_OUT="${LOCAL_OUT:-1}" .venv/bin/python scripts/make_web_data.py

# -Z6/-z13: r8 hexagons are invisible below z6 and sub-pixel detail above z13.
# --drop-densest-as-needed degrades gracefully at state scope instead of
# refusing to build a dense low-zoom tile.
tippecanoe -o web/data/rf.pmtiles --force \
  -Z6 -z13 --drop-densest-as-needed \
  --no-tile-compression \
  -L hexes:web/data/hexes.geojsonl \
  -L towers:web/data/towers.geojsonl \
  -L sites:web/data/sites.geojsonl

rm -f web/data/hexes.geojsonl web/data/towers.geojsonl web/data/sites.geojsonl
ls -lh web/data/rf.pmtiles
