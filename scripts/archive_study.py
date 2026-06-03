#!/usr/bin/env python3
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

# Read config once at import. A missing file is fine (no mirror, default
# base_dir). Malformed JSON or a group/world-writable config file logs a
# warning and the script continues with an empty config so local archiving
# still works. The mode check matches sshd's policy on key files: anyone who
# can write to config.json could redirect the mirror or change base_dir.
try:
    _st = CONFIG_PATH.stat()
    if _st.st_mode & 0o022:  # any group-write or world-write bit set
        print(f"Warning: {CONFIG_PATH} is group/world writable; refusing to load (run: chmod 600 {CONFIG_PATH}).")
        CONFIG = {}
    else:
        CONFIG = json.loads(CONFIG_PATH.read_text())
except FileNotFoundError:
    CONFIG = {}
except json.JSONDecodeError as e:
    print(f"Warning: malformed {CONFIG_PATH}: {e}; ignoring (mirror disabled).")
    CONFIG = {}

# Optional "base_dir" config overrides the default ~. Lets a service account
# (e.g. mradmin) write archives to a system-wide root like /home or /srv/dicom.
# A missing/empty/non-directory value falls back to home rather than
# auto-creating system paths from a typo'd config. Note: `or` (not
# `get(key, default)`) is used so an empty-string value also falls through.
BASE_DIR = Path(CONFIG.get("base_dir") or str(Path.home()))
if not BASE_DIR.is_dir():
    print(f"Warning: base_dir {BASE_DIR} is not a directory; falling back to home.")
    BASE_DIR = Path.home()
GUEST_DIR = BASE_DIR / "guest"

# Non-ASCII (CJK, accented Latin, etc.) is deliberately preserved — NTFS,
# APFS, ext4 and our toolchain (tar, scp, Python) handle UTF-8 natively,
# and stripping it would mangle real patient names.
#
# '-' is the within-item delimiter; '_' is reserved as the between-item
# separator in archive filenames, so literal '_' in input is also remapped
# to '-' to keep filename structure unambiguous.
def sanitize(text):
    """Make text safe as a filename across Windows + Unix."""
    s = re.sub(r'\s+', '-', str(text))
    s = re.sub(r'[<>:"/\\|?*$;\^\x00-\x1f_]', '-', s)
    s = re.sub(r'-+', '-', s)
    # '-' is deliberately NOT in the strip set — leading '-' must survive so
    # the path-traversal guard in process_study() can catch '-rf'-style IDs.
    return s.strip('._ ')

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


def mirror_to_smb(local_path, study_desc):
    """Optionally mirror the archive to an SMB share mounted locally.

    Routing on SMB is by the first word of `study_desc` (the sanitised
    StudyDescription, DICOM tag 0008,1030), NOT by PatientID — SMB shares
    here are lab-collaboration surfaces where server-side ACLs scope
    visibility per-lab-folder. If the first word of `study_desc` contains
    "lab" (case-insensitive substring), the archive lands in
    `<mount>/<first_word_lowercased>/`; otherwise it falls back to
    `<mount>/guest/`. Folder names are lower-cased so all lab folders
    sort/group consistently regardless of how the scanner cased the tag
    (`SophieLab TMS` and `sophielab TMS` both land in `sophielab/`).
    Folder is auto-created on first use. Files are written 0666 (RW for
    everyone); per-lab visibility is enforced at the share/ACL level on
    the SMB server, not via POSIX file permissions.

    The mount itself is managed outside this script (typically /etc/fstab
    with `_netdev,nofail`). If the mount is missing — share offline,
    firewall blocking, network down — we log and skip; local archiving
    and other mirrors are unaffected. A later study will pick up the
    mount once it returns.
    """
    smb_cfg = CONFIG.get("smb") or {}
    mount_point = smb_cfg.get("mount_point")
    if not mount_point:
        return

    mp = Path(mount_point)
    if not mp.is_mount():
        print(f"SMB mirror: {mount_point} is not mounted; skipping.")
        return

    # study_desc is already sanitised (whitespace -> '-'), so split on '-'
    # to recover the original first word.
    first_word = study_desc.split("-")[0] if study_desc else ""
    folder_name = first_word.lower() if "lab" in first_word.lower() else "guest"

    dest = mp / folder_name
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"SMB mirror: could not create {dest}: {e}")
        return

    target = dest / local_path.name
    try:
        shutil.copy(local_path, target)
    except OSError as e:
        print(f"SMB mirror: copy failed: {e}")
        return

    # 0666 unconditionally — per-lab access lives in server-side ACLs.
    # cifs may ignore POSIX chmod entirely; silently fine if it doesn't stick.
    try:
        target.chmod(0o666)
    except OSError:
        pass

    print(f"Mirrored to SMB: {target}")


def process_study(study_dir):
    study_path = Path(study_dir)
    dicom_files = list(study_path.glob("*"))
    if not dicom_files:
        return

    # Read first file for metadata
    try:
        ds = pydicom.dcmread(str(dicom_files[0]))
        patient_id = sanitize(getattr(ds, 'PatientID', ''))
        # patient_id is also appended to the archive filename below; we
        # capture it here before the path-traversal guard so an invalid or
        # missing input leaves id_for_filename empty (omitted from the name)
        # rather than baking "guest" or leading-dash junk into it.
        id_for_filename = patient_id
        # Path-traversal defense in depth: sanitize() already strips leading
        # dots and forbidden chars, but mid-string '..' (e.g. 'foo..bar') and
        # leading '-' survive. Reject those and fall back to guest.
        if not patient_id or patient_id.startswith((".", "-")) or ".." in patient_id:
            patient_id = "guest"
            id_for_filename = ""
        patient_name = sanitize(getattr(ds, 'PatientName', 'unknown'))
        study_desc = sanitize(getattr(ds, 'StudyDescription', ''))
        # Strip non-digits — DICOM rarely has them, but a stray '/' would
        # turn the archive_name into an unintended subdirectory.
        study_date = re.sub(r'[^0-9]', '', str(getattr(ds, 'StudyDate', '00000000')))
        study_time = re.sub(r'[^0-9]', '', str(getattr(ds, 'StudyTime', '000000')))[:6]
    except Exception as e:
        print(f"Error reading DICOM: {e}")
        return

    # Determine destination folder
    dest_folder = BASE_DIR / patient_id
    if not dest_folder.exists():
        dest_folder = GUEST_DIR
    dest_folder.mkdir(parents=True, exist_ok=True)

    # Prepare archive name: YYYYMMDD-hhmmss_name[_studydesc][_id].tar.zst.
    # Items are joined by '_'; within-item compounds (date-time, sanitized
    # whitespace, etc.) use '-'. patient_name, study_desc and id are each
    # appended only when non-empty after sanitize (and, for id, valid —
    # see id_for_filename), so an item that sanitises to '' doesn't leave
    # a stray '__' in the filename.
    parts = [f"{study_date}-{study_time}"]
    for item in (patient_name, study_desc, id_for_filename):
        if item:
            parts.append(item)
    archive_name = "_".join(parts) + ".tar.zst"
    final_path = get_unique_path(dest_folder / archive_name)

    # Create Compressed Archive — add files at the archive root, not under
    # the storescp st_<timestamp>/ parent dir. tarfile.add recurses into
    # subdirs and preserves their names (in case storescp ever produces them).
    print(f"Archiving {study_path} to {final_path}...")
    with open(final_path, 'wb') as f:
        cctx = zstd.ZstdCompressor(level=3)
        with cctx.stream_writer(f) as compressor:
            with tarfile.open(fileobj=compressor, mode='w|') as tar:
                for child in sorted(study_path.iterdir()):
                    tar.add(child, arcname=child.name)

    # Optional mirrors — each is independently configured in config.json and
    # is a no-op when its block is absent or its destination is unreachable.
    # Both can run for the same study if both are configured. SSH routes by
    # PatientID; SMB routes by the first word of StudyDescription
    # (see mirror_to_smb).
    mirror_to_ssh(final_path, patient_id)
    mirror_to_smb(final_path, study_desc)

    # Cleanup: Delete original DICOMs
    shutil.rmtree(study_path)
    print("Cleanup complete.")

if __name__ == "__main__":
    if len(sys.argv) > 1:
        process_study(sys.argv[1])
