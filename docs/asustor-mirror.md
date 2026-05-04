# ASUSTOR ADM as the SSH mirror target

Notes specific to running an ASUSTOR NAS (ADM 4.x, BusyBox userspace) as
the destination of `archive_study.py`'s SSH mirror. None of this is
needed for a generic Linux mirror target — the standard `chgrp + chmod 2775`
guidance in the main README is enough there.

All commands assume an admin SSH user (e.g. `mradmin`) with `sudo` access.


## 1. `/etc/init.d/` is regenerated at every boot — don't put scripts there

ADM rebuilds `/etc/init.d/` from a system template each time the box boots.
A custom `S98foo` script dropped there will work until the next reboot,
then silently disappear. Symptoms: the bind mount described below works
right after you set it up, then is gone after a power cycle.

The persistent boot hook is **`/usr/local/etc/init.d/S??*.sh`**, sourced by
`/etc/script/rcS.pluginsfs` (which is itself called from `/etc/init.d/rcS`).
`/usr/local/` is a symlink to `/volume1/.@plugins/`, which lives on the data
volume and survives reboots and firmware updates.

Files dropped there are run with `set start; . <path>` — i.e. **sourced** in
a subshell with `$1=start`. So:

- the file must end in `.sh` (otherwise ADM treats it as an AppCentral
  package and tries to `apkg --power-on-enable-app` it);
- it should handle `${1:-start}` and `stop` cases;
- absolute paths are safer than relying on `$PATH` at boot.


## 2. Bind-mount `/home/guest` into each lab user's home

The mirror lands archives in `/home/<lab>/` and `/home/guest/`. Lab users
connecting via Samba see only their own home as their share root, so they
can't browse `/home/guest/` directly — and a symlink doesn't help because
ADM's Samba refuses to follow symlinks pointing outside the share by default.

A bind mount makes `/home/guest` look like a regular subdirectory of each
lab home, which Samba serves without any special config:

```sh
mount -o bind /home/guest /home/crlab/guest
```

To prevent the user from `rm`ing the empty mountpoint while it's not
mounted (e.g. between reboot and the boot script firing), set the
underlying directory to `chattr +i`:

```sh
mkdir /home/crlab/guest
chmod 755 /home/crlab/guest
chown root:root /home/crlab/guest
chattr +i /home/crlab/guest
```

Bind mounts work fine over an immutable directory — `+i` only blocks
modifications to the inode itself, not mounts on top of it.


## 3. Reboot-persistent setup

Drop a script like the following at `/usr/local/etc/init.d/S98dicompress-binds.sh`
and `chmod 755` it. Adjust the user list at the bottom for your site.

```sh
#!/bin/sh
# Bind-mount /home/guest into each lab user's home.
# Sourced by /etc/script/rcS.pluginsfs at boot (start) and rcK.pluginsfs (stop).

is_mounted() {
    real=$(/usr/bin/readlink -f "$1") || return 1
    /usr/bin/awk -v p="$real" "\$2 == p { f=1 } END { exit !f }" /proc/mounts
}

ensure_mount() {
    user="$1"
    mp="/home/$user/guest"
    [ -d "/home/$user" ] || return 0
    if [ ! -d "$mp" ]; then
        /bin/mkdir "$mp" || return 1
        /bin/chmod 755 "$mp"
        /bin/chown root:root "$mp"
        /bin/chattr +i "$mp" 2>/dev/null || true
    fi
    is_mounted "$mp" || /bin/mount -o bind /home/guest "$mp"
}

unmount_one() {
    mp="/home/$1/guest"
    real=$(/usr/bin/readlink -f "$mp" 2>/dev/null) || return 0
    while /usr/bin/awk -v p="$real" "\$2 == p { f=1 } END { exit !f }" /proc/mounts; do
        /bin/umount "$mp" || break
    done
}

case "${1:-start}" in
    start) for u in crlab jflab; do ensure_mount  "$u"; done ;;
    stop)  for u in crlab jflab; do unmount_one   "$u"; done ;;
esac
```

Test by sourcing manually first (`set start; . /usr/local/etc/init.d/S98dicompress-binds.sh`),
then verify a real reboot picks it up. The script in `archive_study.py`
itself doesn't care about any of this — it just SCPs to `/home/<lab>/`.


## 4. Per-user folder perms

Default ADM home perms are mode `755` owned by the lab user — only that
user can write. The mirror runs as the admin user, so each folder needs:

```sh
sudo chgrp administrators /home/crlab && sudo chmod 2775 /home/crlab
sudo chgrp administrators /home/jflab && sudo chmod 2775 /home/jflab
sudo chmod 2777 /home/guest                              # world-writable for unknown PatientIDs
```

The setgid bit (`2`) makes new files inherit the directory's group, which
matters because the script doesn't `chown` after SCP — it only `chmod`s.

> **Heads up:** at least on some ADM builds, an internal cron task
> (`usermanutil` and/or `shareroutines`) periodically resets the group on
> user home directories back to `users`. If `/home/<lab>` keeps reverting
> from `administrators` to `users`, that's almost certainly the culprit.
> If cross-lab privacy matters, you'll need to either disable the offending
> cron task or have the receiver re-apply `chgrp` before each SCP.


## 5. Reboot mechanics, briefly

- `/sbin/reboot` (BusyBox) sometimes silently no-ops. Use **`/sbin/reboot -f`**
  for a reliable command-line reboot.
- `/usr/sbin/shutdownctrl` is ADM's native tool but **segfaults with no
  arguments**, and the working subcommands don't appear in `--help`.
- The on-box "ipblockman" plus OpenSSH 9.6's `PerSourcePenalties` will
  briefly reject a source IP after a few failed key attempts. Symptom:
  `kex_exchange_identification: banner line 0: Not allowed at this time`.
  Wait ~30 seconds and retry; or check `/var/log/messages` for
  `add … login 1 to ipblockman` entries to confirm.
