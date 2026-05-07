#!/bin/bash
# Prefix every line of stdout/stderr with an ISO-8601 timestamp. storescp's
# --verbose output and archive_study.py's prints both flow through this
# filter, so the cron-launched log file gets timestamps without needing a
# log4cplus config. fflush keeps writes line-buffered.
exec > >(awk '{ print strftime("%Y-%m-%dT%H:%M:%S"), $0; fflush(); }') 2>&1
set -x  # This will show us exactly where the script stops!

# Use absolute paths for everything
PORT=11112
AETITLE="PY_STORE_SCP"
INCOMING="/tmp/dicom_incoming"
PYTHON_SCRIPT="/usr/local/bin/archive_study.py"
STORES_BIN="/opt/homebrew/bin/storescp"
# PYTHON_BIN=""
PYTHON_BIN="/Users/chris/.pyenv/versions/3.13.2/bin/python3"

# Negotiation flags. --promiscuous accepts unknown SOP classes (so the scanner
# isn't artificially limited); --prefer-lossless steers transfer-syntax
# negotiation toward JPEG Lossless when offered. The SCU still chooses which
# accepted context to USE per Store Request, so on-wire compression only
# kicks in if the scanner is configured to send a compressed transfer syntax
# (typically a per-destination "Compression / Transfer Syntax" option that
# only affects new acquisitions on Siemens systems). See
# https://support.dcmtk.org/docs/storescp.html for the full flag list.
PREFER_TS="--promiscuous --prefer-lossless"

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
