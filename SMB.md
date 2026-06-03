# SMB Mirror

Comprehensive guide to the optional SMB / CIFS mirror destination. The
[README](README.md) has a one-paragraph summary; this file has the full
setup, the gotchas we learned the hard way, the reboot story, and a
troubleshooting catalogue.

DICOMpress can mirror each completed `.tar.zst` to up to three destinations:

```
storescp → archive_study.py → [1] local under BASE_DIR
                            → [2] SSH/SCP to a remote host        (optional)
                            → [3] SMB/CIFS share mounted locally  (optional)
```

Each destination is independently configured; missing or unreachable
destinations are silent no-ops that don't block the others. The SMB
mirror is the most user-friendly for Mac / Windows / domain-managed
environments — once mounted, you write to a path like
`/mnt/dicom-mirror/<PatientID>/<name>.tar.zst` and the file appears in
Finder / Explorer on whichever clients have the share open.


## When to use the SMB mirror

| Scenario | Use SMB? |
|---|---|
| Mirror target is a Mac, Windows, or domain-managed file server | **yes** |
| You want files visible via Samba / Finder / Explorer | **yes** |
| Mirror target is a Linux NAS / VM with SSH access | use [SSH mirror](README.md#optional-mirror-archives-to-a-remote-ssh-server) |
| Both — you want belt-and-braces | configure both blocks |

Either or both mirror blocks can be present in `config.json`; both run
per study if both are configured.


## How destination resolution works

The SMB mirror routes by **the first word of `PerformedProcedureStepDescription`
(DICOM tag `(0040,0254)`)**, NOT by PatientID. This is intentional — SMB
shares are typically lab-collaboration surfaces where server-side ACLs
scope visibility per-lab-folder, so routing by lab name matches the
access model better than routing by patient.

Rule:

> If the **first word** of `PerformedProcedureStepDescription` contains
> the substring **"lab"** (case-insensitive), the archive lands in
> `<mount_point>/<first_word>/`. Otherwise it lands in `<mount_point>/guest/`.

The folder is auto-created on first use, and files are written with
mode `0666` (RW for everyone) — per-lab visibility is enforced at the
share / NTFS-ACL level on the server, not via POSIX file permissions.

`step` is already sanitised by the time `mirror_to_smb` sees it (spaces
and forbidden characters replaced with `-`), so the script splits on
`-` to recover the original first word.

### Examples

| `PerformedProcedureStepDescription` | sanitised `step` | first word | contains "lab"? | lands in |
|---|---|---|---|---|
| `SophieLab TMS` | `SophieLab-TMS` | `SophieLab` | ✓ | `<mount>/SophieLab/…` |
| `BrainHealth AgingBrain` | `BrainHealth-AgingBrain` | `BrainHealth` | ✗ | `<mount>/guest/…` |
| `Research MCBI TESTING` | `Research-MCBI-TESTING` | `Research` | ✗ | `<mount>/guest/…` |
| `Lab` (alone) | `Lab` | `Lab` | ✓ | `<mount>/Lab/…` |
| `Sophielab tms` (lowercase) | `Sophielab-tms` | `Sophielab` | ✓ | `<mount>/Sophielab/…` (case preserved) |
| _(tag missing or empty)_ | `""` | `""` | ✗ | `<mount>/guest/…` |
| `Test Lab Run` (lab not first) | `Test-Lab-Run` | `Test` | ✗ | `<mount>/guest/…` |

Note: PatientID is **not** used for SMB routing. It still appears as the
trailing component of the archive **filename** (e.g.
`20260603-133744_RO_SophieLab-TMS_cr.tar.zst`), but the **folder** comes
from step. The local archive and the SSH mirror (if configured) continue
to route by PatientID — the asymmetry is deliberate.

### Permission caveats

Files inside the share appear owned by the mount user (set via `uid=` in
fstab — typically the receiver's service account like `radmin`). You
**cannot** make `<mount>/SophieLab/foo.tar.zst` appear owned by any
individual user — that's a property of SMB, not a bug. Per-user access
is enforced by **server-side share / NTFS ACLs**, not POSIX ownership.
The `chmod 0666` the script applies is best-effort; cifs may ignore it
in favour of server-side ACLs, which is fine.


## One-time setup (Ubuntu/Debian receiver)

Assumes the receiver runs as a service account (e.g. `radmin`) and the
SMB share is reachable from the receiver host. You'll need root on the
receiver (sudo) to install packages, write the credentials file, and
edit `/etc/fstab`.

### 1. Install `cifs-utils`

```bash
sudo apt install cifs-utils
```

### 2. Store credentials in a root-only file

Use a text editor (not a `tee` heredoc — special characters like `$` `\`
`` ` `` `!` in your password can be mangled by the shell):

```bash
sudo nano /etc/cifs-creds-dicompress
```

The file must contain exactly three lines, no leading/trailing whitespace,
no quotes around the password:

```
username=YOUR_AD_USERNAME
password=YOUR_AD_PASSWORD
domain=YOUR_AD_DOMAIN
```

Then lock it down:

```bash
sudo chmod 600 /etc/cifs-creds-dicompress
sudo chown root:root /etc/cifs-creds-dicompress
```

`mode 600` keeps the password readable only by `root` — the same trust
model as an SSH private key. The file isn't encrypted, but no non-root
user on the receiver can read it.

> **Validate the credentials interactively** before relying on the mount.
> If `smbclient` rejects them, `mount.cifs` will too:
>
> ```bash
> sudo smbclient //FILESERVER.EXAMPLE.COM/share -A /etc/cifs-creds-dicompress
> # success → smb: \>  (type `quit` to exit)
> # failure → NT_STATUS_LOGON_FAILURE
> ```

### 3. Add the `/etc/fstab` line

```
//FILESERVER.EXAMPLE.COM/share  /mnt/dicom-mirror  cifs  credentials=/etc/cifs-creds-dicompress,uid=<svc-user>,gid=<svc-group>,file_mode=0664,dir_mode=02775,_netdev,nofail,x-systemd.automount,x-systemd.mount-timeout=15s  0  0
```

Annotated:

| Option | Why |
|---|---|
| `credentials=…` | Points to the file in Step 2; password never appears in fstab |
| `uid=<svc-user>,gid=<svc-group>` | Files appear owned by the receiver's service account so the script can read/write the mount-point |
| `file_mode=0664,dir_mode=02775` | POSIX appearance from the Linux side (server-side ACLs still rule). **Both values MUST have a leading zero** — see Lesson 2 below |
| `_netdev,nofail` | Wait for network; never block boot if the share is unreachable |
| `x-systemd.automount` | systemd creates a lazy-mount unit; the actual cifs mount happens on first access to the path, with retries on network blips |
| `x-systemd.mount-timeout=15s` | Cap the mount attempt so the first writer doesn't hang for minutes |

Then activate:

```bash
sudo mkdir -p /mnt/dicom-mirror
sudo systemctl daemon-reload
sudo mount /mnt/dicom-mirror     # eager-mount once for testing
mount | grep dicom-mirror        # should show the share with vers=…, dir_mode=02775
ls -la /mnt/dicom-mirror/        # should now list contents
```

### 4. Tell the receiver to use it

Edit `~/.config/dicompress/config.json` on the account that runs the
receiver (e.g. `radmin`'s home). The `smb` block sits alongside any
existing `base_dir` / `ssh` blocks:

```json
{
  "base_dir": "/home",
  "smb": {
    "mount_point": "/mnt/dicom-mirror"
  }
}
```

No restart of `storescp` is needed — `archive_study.py` re-reads the
config on every study (`--exec-on-eostudy` spawns a fresh process).

### 5. End-to-end test

Send a study from any DICOM SCU. Check the receiver log:

```bash
ssh <svc-user>@<receiver> 'tail -10 ~/storescp.log'
```

You should see:

```
Archiving … to /home/<PatientID>/<name>.tar.zst...
Mirrored to SMB: /mnt/dicom-mirror/<PatientID>/<name>.tar.zst
Cleanup complete.
```

…and the same file should appear on any client that has the SMB share
mounted (Mac: Finder; Windows: File Explorer; Linux: `ls /mnt/…`).


## Reboot resilience

### When the receiver reboots

Everything auto-recovers:

| Component | How it survives |
|---|---|
| `storescp` itself | The service account's `@reboot` crontab line fires it when `crond` starts |
| SMB mount | `/etc/fstab` with `_netdev,nofail,x-systemd.automount` — systemd creates an automount unit at boot; the actual cifs mount happens on first access to `/mnt/dicom-mirror/` |
| Credentials | `/etc/cifs-creds-dicompress` is persistent root-owned |
| Config | `~/.config/dicompress/config.json` persists in the user's home |

The `nofail` keyword is what guarantees the receiver still boots even if
the SMB server is unreachable at that moment. The very first study after
reboot triggers `Path.is_mount('/mnt/dicom-mirror')` from the Python
script, which in turn triggers systemd's automount and brings the cifs
session up.

### When the SMB server reboots

The Linux CIFS kernel driver handles reconnection. Our mount options
include `soft` and `echo_interval=60`, meaning:

- During the outage, `shutil.copy` to the mount path raises `OSError`
  (typically `[Errno 5] Input/output error` or `[Errno 107] Transport
  endpoint is not connected`). `mirror_to_smb` catches the error, logs
  it, and returns — local archiving and any other mirrors are unaffected.
- The kernel sends an SMB echo every 60s; once the server is back, the
  session re-establishes silently.
- The next study after the server returns mirrors normally.

### Manual recovery (rare)

If the server is down for hours and the cifs session is fully torn down
in a way the kernel can't recover from automatically:

```bash
sudo umount -lf /mnt/dicom-mirror   # lazy + force — won't hang even if connection is dead
sudo mount /mnt/dicom-mirror
ls /mnt/dicom-mirror/                # should respond
```

After this, subsequent studies mirror normally.


## Troubleshooting

### `NT_STATUS_LOGON_FAILURE` / `STATUS_LOGON_FAILURE`

Server is rejecting the credentials. Diagnose in this order:

1. **Test interactively** to confirm username/password/domain are right:
   ```bash
   smbclient //SERVER/share -U USERNAME -W DOMAIN.EXAMPLE.COM
   ```
   If this fails, the credentials themselves are wrong — re-confirm with
   the account owner / AD admin.

2. **Test the file** with `smbclient -A`:
   ```bash
   sudo smbclient //SERVER/share -A /etc/cifs-creds-dicompress
   ```
   If interactive (step 1) worked but `-A` fails, the file's password
   value differs from what you typed at the prompt — re-edit with `nano`,
   carefully re-type, retry. Avoid `tee` / `echo` heredocs for the
   password line — shell metacharacters mangle silently.

3. **Verify domain format.** Common variants if `-W DOMAIN.EXAMPLE.COM`
   doesn't work:
   - `-W DOMAIN` (short / netbios name)
   - `-U username@domain.example.com` (UPN style, no `-W`)
   - `-U 'DOMAIN.EXAMPLE.COM\username'` (backslash form, single-quoted
     to protect from the shell)

> Linux's `smbclient` and `mount.cifs` default to **`WORKGROUP`** as the
> domain when none is supplied. macOS's `mount_smbfs` auto-discovers via
> Kerberos/AD, which is why the same credentials "just work" on a Mac
> and fail on Linux. You **must** specify the domain on Linux.

### `ls /mnt/dicom-mirror/` returns "Permission denied" but `mount` shows the share mounted

Almost certainly a `dir_mode` that's missing the leading zero. `mount.cifs`
silently parses bare numbers as **decimal**, so `dir_mode=2775` becomes
octal `05327` (`--wx-w-rwx` — owner has no read).

```bash
sudo sed -i 's|dir_mode=2775|dir_mode=02775|' /etc/fstab
sudo umount /mnt/dicom-mirror
sudo systemctl daemon-reload
sudo mount /mnt/dicom-mirror
ls -la /mnt/dicom-mirror/            # should now list contents
```

### `SMB mirror: /mnt/dicom-mirror is not mounted; skipping.`

The script ran `Path.is_mount()` and got `False`. Either the automount
hasn't been triggered yet (rare — usually triggered by the very call
that checks), or the underlying mount has failed.

```bash
sudo systemctl status mnt-dicom\\x2dmirror.automount
sudo systemctl status mnt-dicom\\x2dmirror.mount
sudo dmesg | grep -iE "cifs|smb" | tail -10
```

Common causes: server unreachable, credentials rotated, firewall change.

### `SMB mirror: copy failed: [Errno 13] Permission denied`

Mount-point ownership mismatch — the script runs as `radmin` but the
mount was created with `uid=root` (or `uid=` was omitted). Re-check the
`uid=` / `gid=` options in `/etc/fstab` and remount:

```bash
sudo umount /mnt/dicom-mirror
sudo mount /mnt/dicom-mirror
ls -ld /mnt/dicom-mirror             # owner should match the receiver's service account
```

### `SMB mirror: copy failed: [Errno 5] Input/output error`

CIFS connection broke mid-write (server crashed, network partition).
Usually self-healing — the kernel reconnects on the next operation, so
the next study mirrors normally. If it persists for more than a few
minutes, run the manual-recovery sequence under **Reboot resilience**
above.


## Lessons from a real-world setup

These cost us debugging time during initial setup; documenting them
here so the next operator doesn't repeat them.

### 1. `WORKGROUP` is `smbclient`'s default domain — you must override

The first symptom looks identical to a wrong password:
`NT_STATUS_LOGON_FAILURE`. The clue is the prompt — `[WORKGROUP\rorden]`
means smbclient is trying to authenticate against a workgroup called
`WORKGROUP`. Pass the real AD domain via `-W` (on the command line) or
`domain=` (in the credentials file).

### 2. `dir_mode` without a leading zero is parsed as decimal

`dir_mode=2775` is **not** the octal `02775` you intend — `mount.cifs`
sees decimal 2775 and converts to octal `05327`, which is
`--wx-w-rwx`. The mount appears to succeed; reads fail with "Permission
denied" because owner has no `r`. Watch for the warning line:

```
WARNING: 'dir_mode' not expressed in octal.
```

Always write `dir_mode=02775` (and `file_mode=0664` is fine because
`0664 == decimal 436 == octal 0664` only when you mean it as octal —
again, the leading zero is the marker).

### 3. Use `nano` (or another editor) for the credentials file, never `tee` heredocs

If your password contains `$`, `\`, `` ` ``, `!`, or `"`, even a
single-quoted `tee <<'EOF'` will preserve it correctly — but a non-quoted
heredoc, or `echo`, will mangle them. Easier to just open the file in
`nano` and type the password literally, then verify with `sudo cat`.

### 4. Confirm credentials interactively before relying on the mount

`mount.cifs` errors are uninformative (`mount error(13): Permission
denied`). `smbclient -A /etc/cifs-creds-dicompress //SERVER/share`
gives you a clear yes/no on whether the file is readable by mount.cifs
*and* the credentials are accepted by the server. Always validate this
before debugging mount options.

### 5. macOS "just works", Linux doesn't — they're not the same

```bash
# macOS — works without specifying a domain
mount_smbfs //rorden@codoitrcdatahub.ds.sc.edu/rorden_test ~/Desktop/share

# Linux — fails the same way unless you specify the domain
sudo smbclient //codoitrcdatahub.ds.sc.edu/rorden_test -A /etc/cifs-creds-dicompress
```

macOS uses Kerberos auto-discovery against AD; Linux `cifs-utils`
doesn't. This is not a configuration bug, just a protocol difference.

### 6. `x-systemd.automount` means `mount | grep` is empty until first access

Right after reboot the share won't appear in `mount` output until
*something* touches `/mnt/dicom-mirror`. That's normal — `ls
/mnt/dicom-mirror/` or the first archive write will trigger the
automount and the share appears. Don't confuse "not in mount table yet"
with "broken".

### 7. The `_netdev,nofail` combination is what makes the receiver boot-safe

Without `nofail`, an unreachable SMB server at boot will leave the
system in emergency mode, locking the entire receiver out. With it, the
mount-attempt fails silently, the rest of boot completes, and
`storescp` comes up under cron's `@reboot` — local archiving works,
SMB mirror no-ops, recovery is automatic.
