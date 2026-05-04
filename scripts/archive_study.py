#!/usr/bin/env python3
import os
import sys
import json
import shlex
import subprocess
import pydicom
import tarfile
import shutil
import re
import zstandard as zstd
from datetime import datetime
from pathlib import Path

# --- Configuration ---
TEMP_DICOM_ROOT = Path("/tmp/dicom_incoming") # Should match storescp -od
CONFIG_PATH = Path.home() / ".config" / "dicompress" / "config.json"

# Read config once at import. A malformed JSON config raises here, so a
# typo in the file fails loudly instead of silently disabling features.
try:
    CONFIG = json.loads(CONFIG_PATH.read_text())
except FileNotFoundError:
    CONFIG = {}

# Optional "base_dir" config overrides the default ~. Lets a service account
# (e.g. radmin) write archives to a system-wide root like /home or /srv/dicom.
BASE_DIR = Path(CONFIG.get("base_dir") or str(Path.home()))
GUEST_DIR = BASE_DIR / "guest"

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


# Shared SSH/SCP options. BatchMode=yes prevents password prompts under cron;
# accept-new pins the host key on first contact (run the pubkey-install step
# interactively first so a real human verifies the fingerprint).
SSH_OPTS = [
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=10",
    "-o", "StrictHostKeyChecking=accept-new",
]


def _ssh_base(ssh_cfg):
    return [
        "ssh",
        "-p", str(ssh_cfg.get("port", 22)),
        *SSH_OPTS,
        f"{ssh_cfg['user']}@{ssh_cfg['host']}",
    ]


# Roots to probe for user / guest folders on the remote, in order.
# Covers Synology DSM (/volume1/home), generic Linux (/home), and macOS (/Users).
REMOTE_HOME_ROOTS = ("/volume1/home", "/home", "/Users")


def _resolve_remote_dir(ssh_cfg, patient_id):
    """Returns (remote_dir, is_guest). Probes REMOTE_HOME_ROOTS for an existing
    folder named after patient_id; falls back to a same-named 'guest' folder.
    Does not create folders — same fallback semantics as the local side."""
    pid_q = shlex.quote(patient_id)
    roots = " ".join(shlex.quote(r) for r in REMOTE_HOME_ROOTS)
    remote_cmd = (
        f'pid={pid_q}; '
        f'for root in {roots}; do '
        f'  if [ -d "$root/$pid" ]; then printf "USER:%s\\n" "$root/$pid"; exit 0; fi; '
        f'done; '
        f'for root in {roots}; do '
        f'  if [ -d "$root/guest" ]; then printf "GUEST:%s\\n" "$root/guest"; exit 0; fi; '
        f'done; '
        f'exit 1'
    )
    result = subprocess.run(
        _ssh_base(ssh_cfg) + [remote_cmd],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        msg = result.stderr.strip() or "no matching user or guest folder"
        print(f"SSH mirror: could not resolve remote directory ({msg}).")
        return None, False

    out = result.stdout.strip()
    if out.startswith("USER:"):
        return out[len("USER:"):], False
    if out.startswith("GUEST:"):
        return out[len("GUEST:"):], True
    return None, False


def mirror_to_ssh(local_path, patient_id):
    """Optionally mirror the archive to a remote SSH server. No-op if not configured."""
    ssh_cfg = CONFIG.get("ssh") or {}
    if not ssh_cfg.get("host") or not ssh_cfg.get("user"):
        return

    remote_dir, is_guest = _resolve_remote_dir(ssh_cfg, patient_id)
    if not remote_dir:
        print("SSH mirror: no remote directory resolved; skipping.")
        return

    remote_target = f"{remote_dir.rstrip('/')}/{local_path.name}"
    scp_cmd = [
        "scp",
        "-P", str(ssh_cfg.get("port", 22)),
        *SSH_OPTS,
        str(local_path),
        f"{ssh_cfg['user']}@{ssh_cfg['host']}:{remote_target}",
    ]
    if subprocess.run(scp_cmd).returncode != 0:
        print("SSH mirror: scp failed; skipping chmod.")
        return

    mode = "0666" if is_guest else "0664"
    chmod_cmd = _ssh_base(ssh_cfg) + [f"chmod {mode} {shlex.quote(remote_target)}"]
    if subprocess.run(chmod_cmd).returncode != 0:
        print("SSH mirror: chmod failed.")
        return

    print(f"Mirrored to {ssh_cfg['user']}@{ssh_cfg['host']}:{remote_target}")


def process_study(study_dir):
    study_path = Path(study_dir)
    dicom_files = list(study_path.glob("*"))
    if not dicom_files:
        return

    # Read first file for metadata
    try:
        ds = pydicom.dcmread(str(dicom_files[0]))
        patient_id = sanitize(getattr(ds, 'PatientID', 'guest'))
        # Path-traversal guard: sanitize() leaves '.' alone, so '..', leading
        # dots/dashes, or any embedded '..' could escape ~ locally and the
        # probed home root remotely. Fall back to guest like other invalid IDs.
        if not patient_id or patient_id.startswith((".", "-")) or ".." in patient_id:
            patient_id = "guest"
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

    # Optionally mirror to remote SSH server
    mirror_to_ssh(final_path, patient_id)

    # Cleanup: Delete original DICOMs
    shutil.rmtree(study_path)
    print("Cleanup complete.")

if __name__ == "__main__":
    if len(sys.argv) > 1:
        process_study(sys.argv[1])
