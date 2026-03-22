# DICOM Receiver & Archiver

Automated DICOM reception with PatientID-based sorting and Zstd compression.
Receives studies via `storescp`, then compresses them into `.tar.zst` archives
organised by PatientID under your home directory.

---

## Prerequisites

| Requirement | macOS | Linux (Debian/Ubuntu) |
|---|---|---|
| DCMTK | `brew install dcmtk` | `sudo apt install dcmtk` |
| Python 3 | ships with macOS | `sudo apt install python3 python3-venv` |

---

## Installation

### Step 1 — Clone or download the project

```bash
cd ~
# unzip the archive if you received a .zip, or clone your repo here
```

### Step 2 — Create a Python virtual environment

A virtual environment (venv) solves the "wrong pip / wrong python" problem entirely.
Inside a venv, `python` and `pip` are always the *same* interpreter, and
`which python` gives you the exact path you need for the shell script.

```bash
# Create the venv next to the scripts folder (one-time setup)
python3 -m venv ./venv
```

> **Why `python3 -m venv`?**  
> Running `python3 -m venv` uses whichever `python3` is on your PATH to create
> the environment, so you always know which interpreter owns it. Once activated,
> every `pip install` and every `python` call inside that shell session goes to
> the same interpreter — no mismatch possible.

### Step 3 — Activate the venv and install dependencies

```bash
# Activate (do this once per terminal session)
source ./venv/bin/activate

# Your prompt will now show (venv) to confirm it is active.
# Install dependencies — pip here is guaranteed to match python
pip install -r ./scripts/requirements.txt
```

### Step 4 — Auto-configure the shell script

The script needs two absolute paths: the venv's Python and the `storescp`
binary. Both vary by machine — Homebrew on Apple Silicon puts binaries in
`/opt/homebrew/bin/` rather than `/usr/local/bin/`; Linux differs again.
Run both `sed` commands to set them automatically:

```bash
# Capture paths into variables first (avoids nested quote issues)
PYTHON_PATH=$(./venv/bin/python3 -c 'import sys; print(sys.executable)')
STORES_PATH=$(which storescp)

# Optional: confirm the values look correct before patching
echo "Python: $PYTHON_PATH"
echo "storescp: $STORES_PATH"

# Patch the script
sed -i.bak "s|PYTHON_BIN=.*|PYTHON_BIN=\"$PYTHON_PATH\"|" ./scripts/start_storescp.sh
sed -i.bak "s|STORES_BIN=.*|STORES_BIN=\"$STORES_PATH\"|" ./scripts/start_storescp.sh
```

Verify both were set correctly:

```bash
grep -E "PYTHON_BIN|STORES_BIN" ./scripts/start_storescp.sh
# Expected output (paths will differ by machine):
# STORES_BIN="/opt/homebrew/bin/storescp"    # macOS Apple Silicon
# STORES_BIN="/usr/local/bin/storescp"        # macOS Intel
# STORES_BIN="/usr/bin/storescp"              # Linux
# PYTHON_BIN="/Users/alice/dicom-archiver/venv/bin/python3"
```

If `which storescp` returns nothing, DCMTK is not installed — go back to Step 1.

> **Why `sys.executable` instead of `which python`?**  
> `which` depends on whether the venv is activated in the current shell.
> Asking the venv's Python to report `sys.executable` works regardless of
> activation state and is guaranteed to return the correct path.
>
> **Why `-i.bak`?**  
> macOS (BSD sed) and Linux (GNU sed) handle in-place editing slightly
> differently. The `.bak` suffix satisfies both. A backup is written as
> `start_storescp.sh.bak`; you can delete it once you've verified the result.
>
> After this step the script is fully self-contained. You do **not** need to
> activate the venv before running it — all paths are absolute.

### Step 5 — Set permissions

```bash
chmod +x ./scripts/*.sh ./scripts/*.py
```

### Step 6 — Copy scripts to a system-wide location

```bash
sudo cp ./scripts/start_storescp.sh /usr/local/bin/
sudo cp ./scripts/archive_study.py  /usr/local/bin/
```

### Step 7 — Auto-start on reboot (cron)

```bash
(crontab -l 2>/dev/null; echo "@reboot /bin/bash /usr/local/bin/start_storescp.sh") | crontab -
```

Verify the entry was added:

```bash
crontab -l
```

---

## macOS-specific: Full Disk Access

macOS blocks background processes from writing to your Home folder by default.
Grant access **before** testing:

1. Open **System Settings → Privacy & Security → Full Disk Access**
2. Click **+** and add `/bin/bash`
3. Add `/usr/sbin/cron`
4. Add **Terminal.app** (or whatever terminal emulator you use)

---

## Testing

**1. Start the receiver manually:**

```bash
/usr/local/bin/start_storescp.sh
```

**2. Send a test study** (from another terminal):

```bash
storescu localhost 11112 -aet SCANNER -aec PY_STORE_SCP +sd ./DICOMs
```

**3. Verify output:**

Check for a `.tar.zst` archive in `~/patientid/` (matched PatientID) or `~/guest/`
(unknown PatientID):

```bash
ls -lh ~/guest/
# or
ls -lh ~/<PatientID>/
```

---

## Project layout

```
.
├── README.md
├── venv/                        # created by you in Step 2 (not committed to git)
└── scripts/
    ├── requirements.txt         # pydicom, zstandard
    ├── start_storescp.sh        # launches storescp, set PYTHON_BIN here
    └── archive_study.py         # sorts & compresses each completed study
```

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `ModuleNotFoundError: pydicom` | Script using wrong Python | Confirm `PYTHON_BIN` in `start_storescp.sh` points to the venv |
| `Permission denied` writing to home dir | macOS Full Disk Access | See macOS section above |
| `storescp: command not found` | DCMTK not installed or not on PATH | Reinstall DCMTK; confirm with `which storescp` |
| cron job doesn't run | cron daemon not running | macOS: `sudo cron` is started on demand. Linux: `systemctl status cron` |
| Archive lands in `~/guest/` | PatientID not found on disk | Create `~/<PatientID>/` directory before sending |
