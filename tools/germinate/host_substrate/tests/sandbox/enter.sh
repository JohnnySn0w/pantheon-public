#!/bin/bash
# T-2918 disposable-host sandbox entry. Runs as uid 0 of the account's rootless
# user namespace (podman unshare), inside a NEW private mount namespace
# (unshare --mount --propagation private). Every write below lands in a tmpfs.
#   $1 mount dir (an empty dir under pytest tmp_path)  $2 repo root
#   $3 run nonce                                        $4 sandbox package dir
set -eu
M=$1 REPO=$2 NONCE=$3 PKG=$4
PATH=/usr/sbin:/usr/bin:/sbin:/bin

mount -t tmpfs -o mode=0755 tmpfs "$M"
mkdir -p "$M/etc" "$M/home" "$M/srv" "$M/vlsd/linger" "$M/agent" "$M/repo" "$M/.base"

# private /etc: the readable real files, plus synthesized unreadable ones
cp -r /etc/. "$M/etc/" 2>/dev/null || true
awk -F: '{print $1":*:20000:0:99999:7:::"}' /etc/passwd > "$M/etc/shadow"
awk -F: '{print $1":*::"}' /etc/group > "$M/etc/gshadow"
chmod 0640 "$M/etc/shadow" "$M/etc/gshadow"
printf 'Defaults env_reset\nroot ALL=(ALL:ALL) ALL\n@includedir /etc/sudoers.d\n' > "$M/etc/sudoers"
chmod 0440 "$M/etc/sudoers"
rm -rf "$M/etc/sudoers.d"
mkdir -m 0750 "$M/etc/sudoers.d"
mkdir -p "$M/etc/systemd/system/multi-user.target.wants" "$M/etc/dbus-1/system.d"
printf '%s\n' "$NONCE" > "$M/etc/.pantheon-sandbox-marker"

cp "$PKG/agent.py" "$M/agent/agent.py"
cp "$PKG/fake_loginctl" "$M/agent/loginctl"
cp "$PKG/fake_systemctl" "$M/agent/systemctl"
chmod 0755 "$M/agent/loginctl" "$M/agent/systemctl"
mount --bind "$REPO" "$M/repo"

for d in etc home srv vlsd; do cp -a "$M/$d" "$M/.base/$d"; done
mkdir -m 0700 "$M/staging"
mount -o remount,bind,ro "$M/repo"

# The sandbox tree moves to /mnt FIRST (rbind keeps the read-only repo
# submount, traversable by fixture uids at /mnt/repo), and every overlay below
# is bound FROM /mnt: $M may live under /tmp or /home (wherever pytest's
# basetemp is; CI's is under /opt), and overlaying either would hide it.
mount --rbind "$M" /mnt
mount -o remount,bind,ro /mnt/repo
mount --bind /mnt/etc /etc
mount --bind /mnt/home /home
mount --bind /mnt/srv /srv
mount --bind /mnt/vlsd /var/lib/systemd
mount --bind /mnt/agent/loginctl /usr/bin/loginctl
mount --bind /mnt/agent/systemctl /usr/bin/systemctl
# review N2: /tmp, /var/tmp and /dev/shm become private too
mount -t tmpfs -o mode=1777 tmpfs /tmp
mount -t tmpfs -o mode=1777 tmpfs /var/tmp
mount -t tmpfs -o mode=1777 tmpfs /dev/shm

# run from the sandbox root, never pytest's real cwd: nothing may resolve against
# a real directory (the agent also refuses relative path arguments outright)
cd /mnt
exec /usr/bin/python3 -I /mnt/agent/agent.py "$NONCE" /mnt
