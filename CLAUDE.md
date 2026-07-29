# CLAUDE.md — notes for AI assistants working on this repo

## Architecture

Two cooperating processes:

1. `start_storescp.sh` runs `storescp` (DCMTK) listening on AET `PY_STORE_SCP`,
   port 11112, writing incoming DICOMs into `/tmp/dicom_incoming/<study>/`.
2. On End-Of-Study (5s of silence), storescp invokes
   `archive_study.py <study-dir>` via `--exec-on-eostudy`. The Python side
   reads metadata from the first DICOM, tar+zstd-compresses the study,
   deletes the temp dir, and optionally mirrors the archive to a remote
   SSH/SCP target and/or a locally-mounted SMB share (each configured
   independently in `config.json`).

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
  `StudyDescription` (0008,1030)**, NOT by PatientID. If that word
  contains the substring `lab` (case-insensitive), the archive lands in
  `<mount_point>/<first_word_lowercased>/`; otherwise
  `<mount_point>/guest/`. Folder name is always lowercased so casing
  variants converge (`SophieLab` / `sophielab` / `SOPHIELAB` all → 
  `sophielab/`). The folder is auto-created on first use; files written
  `0666` (per-lab visibility lives in server-side share/NTFS ACLs, not
  POSIX). This is intentionally different from local + SSH
  PatientID-based routing — SMB shares are lab-collaboration surfaces,
  not per-user homes. The mount itself is managed outside the script
  (fstab `_netdev,nofail` + `x-systemd.automount`), so the script
  no-ops when the share is offline rather than blocking local archiving.

The two mirror modes (`ssh` and `smb`) are independently configurable; each
runs only if its block is present in `config.json`, and they can run for
the same study.

## Policies — don't accidentally weaken these

- **Loud-on-bad-config**: malformed `config.json` logs a warning and falls
  back to `CONFIG = {}` (mirror disabled, default `base_dir`). Local archiving
  still works. Don't replace this with a silent swallow that hides typos.
- **Config file mode-check**: `archive_study.py` refuses to load
  `config.json` if its mode has any group-write or world-write bit set
  (`& 0o022`). The check exists because anyone who can write the file
  could redirect the SSH/SMB mirror or change `base_dir`. Same trust
  model as sshd's policy on key files. If a future audit flags this as
  "over-defensive", don't drop it — the threat model is real for shared
  receivers.
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
- **SMB atomic publish (`.part` rename)**: `mirror_to_smb` writes to
  `<target>.part` then `rename`s to `<target>`. Without this, a CIFS
  disconnect mid-copy would leave a partial `.tar.zst` at the final name
  and any client watching the share (Finder / inotify) would see a
  corrupt file. Same-directory rename is atomic on POSIX and on cifs.
  Don't "simplify" back to a direct `shutil.copy(local_path, target)`.
- **`PREFER_TS = "--promiscuous --prefer-lossless"`** in
  `start_storescp.sh` is the validated default. `--promiscuous` accepts
  unknown SOP classes so the scanner isn't artificially limited;
  `--prefer-lossless` steers TS negotiation toward JPEG Lossless when
  the scanner offers it. **Real-world finding**: Siemens XA scanners
  don't transcode at send time — they pick the accepted context that
  matches the file's on-disk encoding. So `--prefer-lossless` is a
  no-op against most XA studies unless the scanner is configured to
  store compressed. Don't conclude the receiver is "broken" if archives
  arrive uncompressed; that's the scanner side.

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
- **SMB mount gotchas** (full detail in [SMB.md](SMB.md)):
  - `dir_mode=02775` in fstab needs the leading zero; `mount.cifs`
    silently parses `dir_mode=2775` as decimal (= octal `05327`,
    `--wx-w-rwx`, owner-no-read), and `ls` returns "Permission denied"
    on the otherwise-mounted share.
  - With `x-systemd.automount` the share doesn't appear in `mount`
    output until first access. That's not a broken mount; touching
    the path (or running the receiver) triggers it.
  - Linux `smbclient` / `mount.cifs` default to domain `WORKGROUP`
    when none is supplied. AD-managed shares fail with
    `NT_STATUS_LOGON_FAILURE` until you pass `-W` / `domain=` in the
    credentials file.

## Deployment state (current production)

Captured here so a future AI assistant clearing context isn't surprised:

- **Receiver host**: Ubuntu 24.04 (codoitrcdatahub-mp15 / `cosomc-mp15dicom`),
  service account `radmin`, venv at `/home/radmin/DICOMpress-venv`,
  scripts deployed at `/usr/local/bin/{start_storescp.sh,archive_study.py}`,
  log at `/home/radmin/storescp.log` (ISO-8601 timestamped), `@reboot`
  cron line in radmin's crontab. `base_dir: "/home"` in config so
  archives land at `/home/<PatientID>/`.
- **Active mirror**: SMB only. `/etc/cifs-creds-dicompress` (mode 600,
  root:root) feeds `/etc/fstab`'s mount of `//codoitrcdatahub.ds.sc.edu/rorden_test`
  at `/mnt/dicom-mirror`. `_netdev,nofail,x-systemd.automount` so the
  receiver boots cleanly when the share is unreachable.
- **SSH mirror feature is in the code/docs but disabled in production**:
  the `"ssh"` block was removed from radmin's `config.json` on the
  receiver. The mechanism is fully retained for other deployments — don't
  delete `mirror_to_ssh`, `_resolve_remote_dir`, or `REMOTE_HOME_ROOTS`
  on the assumption the feature is "unused". Site-specific
  ASUSTOR-target details live in [docs/asustor-mirror.md](docs/asustor-mirror.md).
- **Scanner**: Siemens XA60 (MR), AETitle `SCANNER`, sends to AET
  `PY_STORE_SCP` on port 11112. Real-world studies have
  `StudyDescription` populated when the technologist sets it; some
  non-MR SOP classes leave it empty (those route to `<mount>/guest/`).

## Things to do (if asked) and not to do

- **Avoid** `try / except Exception:` blocks that swallow errors and `return`
  silently — past audits have flagged these and they hide real problems.
- **Avoid** premature abstractions. Three similar lines beats a 5-line
  helper that's only used once. Inline `re.sub` is fine; no need to
  pre-compile patterns for a function called twice per study.
- **Prefer** updating the existing README sections over adding new ones
  for small features — keeps the surface area readable.

## Teams notifications (optional)

- `scripts/teams_notifier.py` posts Legacy-MessageCard webhooks:
  `TEAMS_WEBHOOK_ERROR` on archive failure, `TEAMS_WEBHOOK_LOG` per-study
  success summary. URLs resolve env → `.env` beside the script →
  `~/.config/dicompress/.env`; `check_and_prompt_teams_webhooks()` prompts
  **only when stdin is a TTY** (storescp/cron runs are headless — never add
  an unconditional `input()`). Prompted URLs persist to `.env` mode 600
  (webhook URLs are post-to-channel credentials).
- `send_teams_alert()` deliberately catches all exceptions — the notifier
  must never break archiving. This is a sanctioned exception to the
  no-silent-swallow policy below, but it still prints to the console.
- The `__main__` wrapper in `archive_study.py` re-raises after sending the
  error alert so storescp's log keeps the full traceback — don't swallow it.
- Success summaries contain the archive filename (embeds the PatientName/ID
  tags). In the current deployment these are study codes, not real patient
  names, so this is fine — but the README notes the caveat for sites that
  send real identifiers; don't add more tag values to the log payload
  casually.

## Files

- `scripts/archive_study.py` — the Python side, run per study.
- `scripts/teams_notifier.py` — optional Teams webhook alerts (stdlib only),
  imported by `archive_study.py`; deploy the two files together.
- `scripts/start_storescp.sh` — launches storescp; `PYTHON_BIN`/`STORES_BIN`
  are absolute paths edited at install time.
- `scripts/config.example.json` — template for `~/.config/dicompress/config.json`.
- `DICOMs/MR.dcm` — single demo DICOM (PatientID=crlab) for the README's
  Testing section.
- `SMB.md` — deep dive on the optional SMB mirror destination (setup,
  reboot semantics, the real-world traps we hit during deployment).
  README has a brief summary; SMB.md is the reference.
- `docs/asustor-mirror.md` — same role for the SSH-mirror-to-ASUSTOR path.
