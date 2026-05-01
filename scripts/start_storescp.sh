#!/bin/bash
set -x  # This will show us exactly where the script stops!

# Use absolute paths for everything
PORT=11112
AETITLE="PY_STORE_SCP"
INCOMING="/tmp/dicom_incoming"
PYTHON_SCRIPT="/usr/local/bin/archive_study.py"
STORES_BIN="/opt/homebrew/bin/storescp"
# PYTHON_BIN=""
PYTHON_BIN="/Users/chris/.pyenv/versions/3.13.2/bin/python3"

# Preferred network transfer syntax — leave empty for storescp's default
# (uncompressed). See https://support.dcmtk.org/docs/storescp.html for the
# full list (e.g. --prefer-little, --prefer-big, --prefer-jpeg8,
# --prefer-j2k-lossless, --prefer-rle, --accept-all).
PREFER_TS="--prefer-lossless"

mkdir -p "$INCOMING"


pkill -x storescp || true
echo "Starting storescp..."

# We removed --fork and added --verbose to see the direct error
$STORES_BIN $PORT \
    --aetitle "$AETITLE" \
    --output-directory "$INCOMING" \
    --sort-conc-studies "st" \
    --eostudy-timeout 5 \
    --exec-on-eostudy "$PYTHON_BIN $PYTHON_SCRIPT #p" \
    $PREFER_TS \
    --verbose
