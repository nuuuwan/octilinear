#!/usr/bin/env bash
# Regenerate every image referenced in README.md.
# Run from the repo root: bash workflows/pipeline.sh

set -euo pipefail

# --- Clean slate ---
rm -rf images data/generate
mkdir -p data/generate/filtered data/generate/simplified data/generate/octilinear
mkdir -p images/octilinear

SCRIPT=workflows/pipeline.py

python3 "$SCRIPT" data/original/provinces.topojson \
    --segment-length 89 --min-area 200 --angle-step 45

python3 "$SCRIPT" data/original/districts.topojson \
    --segment-length 89 --min-area 200 --angle-step 45

python3 "$SCRIPT" data/original/districts.topojson \
    --segment-length 89 --min-area 200 --angle-step 60

python3 "$SCRIPT" data/original/districts.topojson \
    --segment-length 89 --min-area 200 --angle-step 90
