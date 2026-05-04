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
- **REMOTE**: probes `REMOTE_HOME_ROOTS` (`/volume1/home`, `/home`, `/Users`)
  for `<root>/<PatientID>`, falls back to `<root>/guest`. Deliberately does
  NOT use `getent passwd` — Synology and ASUSTOR return `/nonexistent` for
  the system `guest` user, which is *not* the same path as `/volume1/home/guest`.

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
- **`StrictHostKeyChecking=accept-new`** in SSH options pins the host key on
  first contact — fine for cron-launched receivers as long as the user-side
  pubkey-install step is done interactively first.

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
