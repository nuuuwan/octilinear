#!/usr/bin/env bash
# Regenerate every image referenced in README.md.
# Run from the repo root: bash workflows/pipeline.sh

set -euo pipefail

SCRIPT=workflows/pipeline.py

# --- Provinces — baseline ---
python3 "$SCRIPT" data/original/provinces.topojson \
    --segment-length 0.8 --min-area 200 --angle-step 45

# --- Districts — effect of --segment-length ---
python3 "$SCRIPT" data/original/districts.topojson \
    --segment-length 0.8 --min-area 200 --angle-step 45

python3 "$SCRIPT" data/original/districts.topojson \
    --segment-length 0.4 --min-area 200 --angle-step 45

# --- Districts — effect of --min-area ---
python3 "$SCRIPT" data/original/districts.topojson \
    --segment-length 0.8 --min-area 50 --angle-step 45

# (districts --segment-length 0.8 --min-area 200 --angle-step 45 already run above)

# --- Districts — effect of --angle-step ---
python3 "$SCRIPT" data/original/districts.topojson \
    --segment-length 0.8 --min-area 200 --angle-step 90

python3 "$SCRIPT" data/original/districts.topojson \
    --segment-length 0.8 --min-area 200 --angle-step 60

# (angle-step 45 already run above)

# --- Data examples section ---
python3 "$SCRIPT" data/original/countrys.topojson \
    --segment-length 0.8 --min-area 200 --angle-step 45

# (provinces already run above)
# (districts already run above)

python3 "$SCRIPT" data/original/dsds.topojson \
    --segment-length 0.8 --min-area 200 --angle-step 45
