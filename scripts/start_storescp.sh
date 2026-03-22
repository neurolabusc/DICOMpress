#!/bin/bash
set -x  # This will show us exactly where the script stops!

# Use absolute paths for everything
PORT=11112
AETITLE="PY_STORE_SCP"
INCOMING="/tmp/dicom_incoming"
PYTHON_SCRIPT="/usr/local/bin/archive_study.py"
STORES_BIN="/opt/homebrew/bin/storescp"
# PYTHON_BIN=""
PYTHON_BIN=""

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
    --verbose
