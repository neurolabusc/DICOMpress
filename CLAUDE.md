# CLAUDE.md — notes for AI assistants working on this repo

## Architecture

Two cooperating processes:

1. `start_storescp.sh` runs `storescp` (DCMTK) listening on AET `PY_STORE_SCP`,
   port 11112, writing incoming DICOMs into `/tmp/dicom_incoming/<study>/`.
2. On End-Of-Study (5s of silence), storescp invokes
   `archive_study.py <study-dir>` via `--exec-on-eostudy`. The Python side
   reads metadata from the first DICOM, tar+zstd-compresses the study,
   deletes the temp dir, and optionally mirrors via `scp`.

`archive_study.py` is invoked **once per study** as a fresh process, so
module-level code (config load, BASE_DIR resolution) runs every time.

## Fallback semantics — note the deliberate asymmetry

- **LOCAL**: archive lands in `BASE_DIR / <PatientID>/` only if that directory
  *already exists*. Otherwise `BASE_DIR / guest/` (auto-created if missing).
  `BASE_DIR` is `~` unless `config.json` overrides via `"base_dir"`.
- **REMOTE (SSH mirror)**: probes `REMOTE_HOME_ROOTS` (`/volume1/home`,
  `/home`, `/Users`) for `<root>/<PatientID>`, falls back to `<root>/guest`.
  Deliberately does NOT use `getent passwd` — Synology and ASUSTOR return
  `/nonexistent` for the system `guest` user, which is *not* the same path
  as `/volume1/home/guest`.
- **REMOTE (SMB mirror)**: routes by **the first word of
  `PerformedProcedureStepDescription`**, NOT by PatientID. If that word
  contains the substring `lab` (case-insensitive), the archive lands in
  `<mount_point>/<first_word>/`; otherwise `<mount_point>/guest/`. The
  folder is auto-created on first use; files written `0666` (per-lab
  visibility lives in server-side share/NTFS ACLs, not POSIX). This is
  intentionally different from local + SSH PatientID-based routing — SMB
  shares are lab-collaboration surfaces, not per-user homes. The mount
  itself is managed outside the script (fstab `_netdev,nofail` +
  `x-systemd.automount`), so the script no-ops when the share is offline
  rather than blocking local archiving.

The two mirror modes (`ssh` and `smb`) are independently configurable; each
runs only if its block is present in `config.json`, and they can run for
the same study.

## Policies — don't accidentally weaken these

- **Loud-on-bad-config**: malformed `config.json` logs a warning and falls
  back to `CONFIG = {}` (mirror disabled, default `base_dir`). Local archiving
  still works. Don't replace this with a silent swallow that hides typos.
- **No silent system-root creation**: `BASE_DIR` is validated at module load.
  A missing directory triggers a warning and fallback to `Path.home()` rather
  than `mkdir -p` materializing `/srv/dicom` from a typo.
- **Path-traversal guard** in `process_study()` is independent of `sanitize()`.
  `sanitize()` strips leading dots and forbidden chars; the guard catches
  mid-string `..`, leading `-`, and empty input. Both are needed — don't
  consolidate them into one.
- **Non-ASCII preservation** in `sanitize()` is intentional. CJK and accented
  patient names round-trip through tar/scp/ext4/APFS/NTFS fine. Don't "fix"
  it to ASCII-only.
- **Two-tier filename delimiters**: archive filenames join items with `_`
  and use `-` for within-item joins (date/time, sanitized whitespace).
  `sanitize()` therefore actively maps literal `_` in DICOM tag values to
  `-`. A future "consistency" change that lets `_` survive sanitize will
  silently break filename parsing on `_` boundaries.
- **`StrictHostKeyChecking=accept-new`** in SSH options pins the host key on
  first contact — fine for cron-launched receivers as long as the user-side
  pubkey-install step is done interactively first.
- **SMB ownership model**: every file the receiver writes through an SMB
  mount appears owned by the mount user (the `uid=…` in fstab). You cannot
  give per-patient POSIX ownership through a single mount — it's a property
  of the SMB protocol, not a bug. Per-user access is enforced via server-side
  share/NTFS ACLs, not POSIX. Don't reintroduce a symlink-to-guest scheme:
  SMB clients (Windows in particular) handle symlinks inconsistently.

## Common gotchas

- `storescu localhost 11112 … +sd ./DICOMs` aborts on macOS because Finder
  leaves `.DS_Store` in the directory. Use a glob: `./DICOMs/*.dcm`.
- The bundled demo (`PatientID=crlab`) lands in `~/guest/` unless `~/crlab/`
  exists first — fallback is by directory existence, not user existence.
- `/etc/init.d/` may be regenerated at boot on appliance OSes (e.g. ASUSTOR
  ADM). For mirror-target setup that needs persistence, use vendor-specific
  hooks like `/usr/local/etc/init.d/*.sh` (sourced by `rcS.pluginsfs` on ADM).
- The script is run via `--exec-on-eostudy` as a fresh process per study,
  so changes to `config.json` take effect on the next study. No restart
  of `storescp` needed.
- Old archives (pre-flatten) have an `st_<timestamp>/` wrapper directory
  inside the tar; new ones extract files at the archive root. The change
  was deliberate — don't reintroduce the wrapper. Both layouts coexist
  on disk for sites that have been running across the change.

## Things to do (if asked) and not to do

- **Avoid** `try / except Exception:` blocks that swallow errors and `return`
  silently — past audits have flagged these and they hide real problems.
- **Avoid** premature abstractions. Three similar lines beats a 5-line
  helper that's only used once. Inline `re.sub` is fine; no need to
  pre-compile patterns for a function called twice per study.
- **Prefer** updating the existing README sections over adding new ones
  for small features — keeps the surface area readable.

## Files

- `scripts/archive_study.py` — the Python side, run per study.
- `scripts/start_storescp.sh` — launches storescp; `PYTHON_BIN`/`STORES_BIN`
  are absolute paths edited at install time.
- `scripts/config.example.json` — template for `~/.config/dicompress/config.json`.
- `DICOMs/MR.dcm` — single demo DICOM (PatientID=crlab) for the README's
  Testing section.
- `SMB.md` — deep dive on the optional SMB mirror destination (setup,
  reboot semantics, the real-world traps we hit during deployment).
  README has a brief summary; SMB.md is the reference.
- `docs/asustor-mirror.md` — same role for the SSH-mirror-to-ASUSTOR path.
