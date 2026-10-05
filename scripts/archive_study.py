#!/usr/bin/env python3
import os
import sys
import json
import fcntl
import shlex
import subprocess
import pydicom
import tarfile
import shutil
import re
import zstandard as zstd
from datetime import datetime, timezone
from pathlib import Path

# Deployed alongside this script (both live in scripts/ and are copied to
# /usr/local/bin together) — Python resolves the import via the script's dir.
from teams_notifier import check_and_prompt_teams_webhooks, send_teams_alert

# --- Configuration ---
TEMP_DICOM_ROOT = Path("/tmp/dicom_incoming") # Should match storescp -od
CONFIG_PATH = Path.home() / ".config" / "dicompress" / "config.json"
# Mirror attempts that failed are queued here (one JSON object per line) and
# retried at the end of every later study run, or via `--retry-mirrors`.
RETRY_QUEUE_PATH = CONFIG_PATH.with_name("pending-mirrors.jsonl")

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

    stem = target_path.name.removesuffix('.tar.zst')
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


class _FileLock:
    """flock on a 0600 lock file beside the retry queue.

    `_FileLock("queue")` serialises reads/writes of the queue file itself;
    concurrent --exec-on-eostudy processes (two studies finishing within
    seconds of each other) must not interleave writes. It is held only for
    the few milliseconds of a read-modify-write.

    `_FileLock("retry", blocking=False)` is a separate, long-held lock that
    makes retry passes mutually exclusive without blocking enqueues: a slow
    SMB transfer in one process must not stall another study's cleanup.
    Non-blocking: `.acquired` is False when another pass is already running.
    """

    def __init__(self, name, blocking=True):
        self._path = RETRY_QUEUE_PATH.with_name(f"pending-mirrors.{name}.lock")
        self._blocking = blocking
        self.acquired = False

    def __enter__(self):
        # The parent is ~/.config/dicompress — the same dir as config.json.
        # 0700 if we have to create it; an existing dir is left alone.
        RETRY_QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | (0 if self._blocking else fcntl.LOCK_NB))
            self.acquired = True
        except BlockingIOError:
            self.acquired = False
        return self

    def __exit__(self, *exc):
        if self.acquired:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        os.close(self._fd)


def _read_queue():
    if not RETRY_QUEUE_PATH.exists():
        return []
    entries = []
    for line in RETRY_QUEUE_PATH.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError as e:
            print(f"Retry queue: dropping malformed line {line!r}: {e}")
    return entries


def _write_queue(entries):
    """Atomically replace the queue file, mode 0600.

    The queue holds archive paths and routing keys (StudyDescription /
    PatientID), so it must not be world-readable; and a crash or disk error
    mid-write must not leave it truncated — the local archives would still
    exist but nothing would remember to mirror them. Write a 0600 temp file
    beside it, fsync, then os.replace (atomic on the same filesystem).
    Callers hold _FileLock("queue").
    """
    tmp = RETRY_QUEUE_PATH.with_suffix(".jsonl.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write("".join(json.dumps(e) + "\n" for e in entries))
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, RETRY_QUEUE_PATH)


def _enqueue_retry(mirror, local_path, key):
    """Record a failed mirror for later retry. Returns the new queue length."""
    with _FileLock("queue"):
        entries = _read_queue()
        entries.append({
            "mirror": mirror,
            "archive": str(local_path),
            "key": key,
            "queued": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })
        _write_queue(entries)
        return len(entries)


def _mirror_failed(message, local_path, mirror, key, retrying):
    """Log a mirror failure; on a first attempt also queue it and raise it to
    the Teams error channel.

    A mirror that silently skips looks identical to one that succeeded from
    the Teams side (the per-study success summary still posts), which let
    un-mirrored archives go unnoticed in production. Every non-success exit
    from mirror_to_ssh / mirror_to_smb must come through here.

    On a retry (`retrying=True`) the entry is already in the queue and the
    operator was already alerted, so we only print — otherwise every later
    study would re-alert for every pending archive while the share is down.
    """
    print(message)
    if retrying:
        return False
    pending = _enqueue_retry(mirror, local_path, key)
    send_teams_alert(
        f"{message} (archive kept locally at {local_path}; "
        f"queued for retry, {pending} pending)",
        level="error",
    )
    return False


def _mirror_succeeded(mirror, local_path, target, retrying):
    """Log a successful mirror and post the per-archive "Transferred" entry.

    The transfer log gets two entries per archive: "Received" when the local
    .tar.zst is written (process_study) and "Transferred" when it lands on a
    mirror (here). Retries post the same entry, tagged, so a reader can
    pair every Received with its Transferred regardless of outages.
    """
    print(f"Mirrored to {mirror.upper()}: {target}")
    tag = " (retried from queue)" if retrying else ""
    send_teams_alert(f"Transferred: {local_path.name} -> {mirror.upper()} {target}{tag}", level="log")
    return True


def mirror_to_ssh(local_path, patient_id, retrying=False):
    """Optionally mirror the archive to a remote SSH server.

    Returns None when not configured, True on success, False on failure
    (after alerting + queueing via _mirror_failed, unless `retrying`).
    """
    ssh_cfg = CONFIG.get("ssh") or {}
    if not ssh_cfg.get("host") or not ssh_cfg.get("user"):
        return None

    def failed(message):
        return _mirror_failed(message, local_path, "ssh", patient_id, retrying)

    remote_dir, is_guest = _resolve_remote_dir(ssh_cfg, patient_id)
    if not remote_dir:
        return failed("SSH mirror: no remote directory resolved; skipping.")

    remote_target = f"{remote_dir.rstrip('/')}/{local_path.name}"
    scp_cmd = [
        "scp",
        "-P", str(ssh_cfg.get("port", 22)),
        *SSH_OPTS,
        str(local_path),
        f"{ssh_cfg['user']}@{ssh_cfg['host']}:{remote_target}",
    ]
    if subprocess.run(scp_cmd).returncode != 0:
        return failed("SSH mirror: scp failed; skipping chmod.")

    mode = "0666" if is_guest else "0664"
    chmod_cmd = _ssh_base(ssh_cfg) + [f"chmod {mode} {shlex.quote(remote_target)}"]
    if subprocess.run(chmod_cmd).returncode != 0:
        return failed(f"SSH mirror: chmod failed on {remote_target}.")

    return _mirror_succeeded(
        "ssh", local_path, f"{ssh_cfg['user']}@{ssh_cfg['host']}:{remote_target}", retrying
    )


def mirror_to_smb(local_path, study_desc, retrying=False):
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
    firewall blocking, network down — we log, fire the Teams error
    webhook, and skip; local archiving and other mirrors are unaffected.
    The failed archive is queued (see RETRY_QUEUE_PATH) and retried at the
    end of every later study run and by `--retry-mirrors`, so it is mirrored
    automatically once the share returns.

    Returns None when not configured, True on success, False on failure
    (after alerting + queueing via _mirror_failed, unless `retrying`).
    """
    smb_cfg = CONFIG.get("smb") or {}
    mount_point = smb_cfg.get("mount_point")
    if not mount_point:
        return None

    def failed(message):
        return _mirror_failed(message, local_path, "smb", study_desc, retrying)

    mp = Path(mount_point)
    if not mp.is_mount():
        return failed(f"SMB mirror: {mount_point} is not mounted; skipping.")

    # study_desc is already sanitised (whitespace -> '-'), so split on '-'
    # to recover the original first word. The '..' guard is defence-in-depth:
    # sanitize() doesn't strip mid-string dots, so a future tweak could
    # otherwise enable folder names like 'sophie..lab' as a path-component.
    lowered = (study_desc or "").split("-")[0].lower()
    folder_name = lowered if "lab" in lowered and ".." not in lowered else "guest"

    dest = mp / folder_name
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return failed(f"SMB mirror: could not create {dest}: {e}")

    # Atomic publish: write to <name>.part then rename. Without this, a CIFS
    # disconnect mid-copy would leave a partial .tar.zst at the final name,
    # and any client watching the share (Finder / inotify) would see a
    # corrupt file. Same-directory rename is atomic on POSIX and on cifs.
    target = dest / local_path.name
    tmp = dest / (local_path.name + ".part")
    try:
        shutil.copy(local_path, tmp)
        tmp.rename(target)
    except OSError as e:
        # Best-effort cleanup of the .part file. During a CIFS outage the
        # unlink can fail too; that must not mask the original copy error
        # or bypass _mirror_failed() by escaping to the top-level handler.
        try:
            tmp.unlink(missing_ok=True)
        except OSError as cleanup_err:
            print(f"SMB mirror: could not remove {tmp}: {cleanup_err}")
        return failed(f"SMB mirror: copy to {target} failed: {e}")

    # 0666 unconditionally — per-lab access lives in server-side ACLs.
    # cifs may ignore POSIX chmod entirely; silently fine if it doesn't stick.
    try:
        target.chmod(0o666)
    except OSError:
        pass

    return _mirror_succeeded("smb", local_path, target, retrying)


MIRROR_FUNCS = {"ssh": mirror_to_ssh, "smb": mirror_to_smb}


def retry_pending_mirrors():
    """Re-attempt every queued mirror. Returns (succeeded, still_pending).

    Locking: the queue lock is taken only to snapshot the entries and again
    to remove the finished ones, never across the mirror attempts
    themselves — a slow or stuck SMB/scp transfer must not block another
    study process from enqueueing its own failure (and so from finishing
    its cleanup). A separate non-blocking "retry" lock makes passes mutually
    exclusive, so entries are never removed by anyone else between our
    snapshot and our removal; the only concurrent change possible is an
    append, which the subtract-by-identity below preserves. If a pass is
    already running we skip rather than wait.

    Entries whose local archive has vanished, or whose mirror is no longer
    configured, are dropped with a Teams error so the operator knows that
    archive needs manual handling. Failures stay queued and print only (the
    original alert already fired). Each success posts its own "Transferred"
    log entry from _mirror_succeeded.
    """
    with _FileLock("retry", blocking=False) as retry_lock:
        with _FileLock("queue"):
            entries = _read_queue()
        if not entries:
            return [], 0
        if not retry_lock.acquired:
            print(f"Retry queue: another retry pass is running; {len(entries)} pending.")
            return [], len(entries)

        print(f"Retry queue: {len(entries)} pending mirror(s).")
        succeeded, finished = [], set()
        for entry in entries:
            mirror, archive, key = entry.get("mirror"), entry.get("archive"), entry.get("key")
            local_path = Path(archive or "")
            func = MIRROR_FUNCS.get(mirror)
            if func is None:
                send_teams_alert(
                    f"Retry queue: unknown mirror {mirror!r} for {archive}; dropping entry.",
                    level="error",
                )
                finished.add((mirror, archive))
                continue
            if not local_path.is_file():
                send_teams_alert(
                    f"Retry queue: local archive {archive} no longer exists; "
                    f"dropping {mirror} retry.",
                    level="error",
                )
                finished.add((mirror, archive))
                continue
            result = func(local_path, key, retrying=True)
            if result is None:
                send_teams_alert(
                    f"Retry queue: {mirror} mirror is no longer configured; "
                    f"dropping retry for {archive} (copy it by hand if still wanted).",
                    level="error",
                )
                finished.add((mirror, archive))
            elif result:
                succeeded.append(f"{local_path.name} -> {mirror.upper()}")
                finished.add((mirror, archive))
            # else: still failing — leave it in the queue.

        with _FileLock("queue"):
            remaining = [
                e for e in _read_queue()
                if (e.get("mirror"), e.get("archive")) not in finished
            ]
            _write_queue(remaining)

    return succeeded, len(remaining)


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
        station_name = sanitize(getattr(ds, 'StationName', ''))
        study_desc = sanitize(getattr(ds, 'StudyDescription', ''))
        # Strip non-digits — DICOM rarely has them, but a stray '/' would
        # turn the archive_name into an unintended subdirectory.
        study_date = re.sub(r'[^0-9]', '', str(getattr(ds, 'StudyDate', '00000000')))
        study_time = re.sub(r'[^0-9]', '', str(getattr(ds, 'StudyTime', '000000')))[:6]
    except Exception as e:
        print(f"Error reading DICOM: {e}")
        # The study dir is left in place for manual recovery, so this return
        # is otherwise invisible in Teams — alert explicitly.
        send_teams_alert(f"Error reading DICOM metadata in {study_path}: {e}", level="error")
        return

    # Determine destination folder
    dest_folder = BASE_DIR / patient_id
    if not dest_folder.exists():
        dest_folder = GUEST_DIR
    dest_folder.mkdir(parents=True, exist_ok=True)

    # Prepare archive name:
    # YYYYMMDD-hhmmss_name[_station][_studydesc][_id].tar.zst.
    # Items are joined by '_'; within-item compounds (date-time, sanitized
    # whitespace, etc.) use '-'. patient_name, station_name, study_desc and
    # id are each appended only when non-empty after sanitize (and, for id,
    # valid — see id_for_filename), so an item that sanitises to '' doesn't
    # leave a stray '__' in the filename.
    parts = [f"{study_date}-{study_time}"]
    for item in (patient_name, station_name, study_desc, id_for_filename):
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

    # Transfer-log entry 1 of 2: the study is safely archived locally. Posted
    # before the mirrors run so it always precedes that archive's
    # "Transferred" entry (entry 2 of 2, posted by _mirror_succeeded).
    size_mb = final_path.stat().st_size / 1_000_000
    send_teams_alert(
        f"Received: {final_path.name} ({len(dicom_files)} file(s), {size_mb:.1f} MB) -> {final_path}",
        level="log",
    )

    # Optional mirrors — each is independently configured in config.json and
    # is a no-op when its block is absent or its destination is unreachable.
    # Both can run for the same study if both are configured. SSH routes by
    # PatientID; SMB routes by the first word of StudyDescription
    # (see mirror_to_smb).
    # Each returns None (not configured) / True / False; failures have already
    # fired the Teams error webhook inside _mirror_failed; successes have
    # posted their "Transferred" log entry. The outcome is also stamped on
    # the console summary.
    mirror_results = {
        "SSH": mirror_to_ssh(final_path, patient_id),
        "SMB": mirror_to_smb(final_path, study_desc),
    }

    # Cleanup: Delete original DICOMs
    shutil.rmtree(study_path)
    print("Cleanup complete.")

    summary = f"Archived {len(dicom_files)} file(s) to {final_path} ({size_mb:.1f} MB)"
    for name, ok in mirror_results.items():
        if ok is not None:
            summary += f"; {name} mirror {'OK' if ok else 'FAILED (queued for retry)'}"

    # Drain earlier failures now that this study is done. A share that came
    # back since the last study gets its backlog without any cron involvement.
    succeeded, pending = retry_pending_mirrors()
    if succeeded:
        summary += f"; retried {len(succeeded)} queued mirror(s) OK"
    if pending:
        summary += f"; {pending} mirror(s) still pending retry"
    return summary

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--retry-mirrors":
        # Cron/manual entry point: drain the retry queue without a study.
        check_and_prompt_teams_webhooks()
        succeeded, pending = retry_pending_mirrors()
        print(f"Retry: {len(succeeded)} succeeded, {pending} still pending.")
    elif len(sys.argv) > 1:
        check_and_prompt_teams_webhooks()
        try:
            summary = process_study(sys.argv[1])
        except Exception as e:
            send_teams_alert(f"{type(e).__name__}: {e} (study dir: {sys.argv[1]})", level="error")
            raise  # keep the loud traceback in storescp's log
        if summary:
            print(summary)
