#!/usr/bin/env python3
import os
import sys
import pydicom
import tarfile
import shutil
import re
import zstandard as zstd
from datetime import datetime
from pathlib import Path

# --- Configuration ---
BASE_DIR = Path.home()
GUEST_DIR = BASE_DIR / "guest"
TEMP_DICOM_ROOT = Path("/tmp/dicom_incoming") # Should match storescp -od

def sanitize(text):
    """Replaces illegal filesystem characters with underscores."""
    # This pattern matches < > : " / \ | ? * $ ; and any non-printable chars
    illegal_chars = r'[<>:"/\\|?*$;\x00-\x1f]'
    # We also include the DICOM caret '^' since you're already replacing that
    clean_text = re.sub(illegal_chars + r'|\^', '_', str(text))
    return clean_text.strip()
    
def get_unique_path(target_path):
    """Appends a, b, c suffix if file exists."""
    if not target_path.exists():
        return target_path
    
    stem = target_path.name.split('.tar.zst')[0]
    ext = ".tar.zst"
    counter = 0
    suffixes = "abcdefghijklmnopqrstuvwxyz"
    
    while True:
        suffix = suffixes[counter] if counter < len(suffixes) else str(counter)
        new_path = target_path.parent / f"{stem}_{suffix}{ext}"
        if not new_path.exists():
            return new_path
        counter += 1

def process_study(study_dir):
    study_path = Path(study_dir)
    dicom_files = list(study_path.glob("*"))
    if not dicom_files:
        return

    # Read first file for metadata
    try:
        ds = pydicom.dcmread(str(dicom_files[0]))
        patient_id = sanitize(getattr(ds, 'PatientID', 'guest'))
        patient_name = sanitize(getattr(ds, 'PatientName', 'unknown'))
        study_date = str(getattr(ds, 'StudyDate', '00000000'))
        study_time = str(getattr(ds, 'StudyTime', '000000'))[:6]
    except Exception as e:
        print(f"Error reading DICOM: {e}")
        return

    # Determine destination folder
    dest_folder = BASE_DIR / patient_id
    if not dest_folder.exists():
        dest_folder = GUEST_DIR
    dest_folder.mkdir(parents=True, exist_ok=True)

    # Prepare archive name: YYYYMMDD_hhmmss_name.tar.zst
    archive_name = f"{study_date}_{study_time}_{patient_name}.tar.zst"
    final_path = get_unique_path(dest_folder / archive_name)

    # Create Compressed Archive
    print(f"Archiving {study_path} to {final_path}...")
    with open(final_path, 'wb') as f:
        cctx = zstd.ZstdCompressor(level=3)
        with cctx.stream_writer(f) as compressor:
            with tarfile.open(fileobj=compressor, mode='w|') as tar:
                tar.add(study_path, arcname=os.path.basename(study_path))

    # Cleanup: Delete original DICOMs
    shutil.rmtree(study_path)
    print("Cleanup complete.")

if __name__ == "__main__":
    if len(sys.argv) > 1:
        process_study(sys.argv[1])
