#!/bin/sh
# Run as root on the Proxmox host:
#   sh provision-guest.sh <CTID> <public-key-file> [docker]
#
# Prepares an LXC guest so Wardn can inspect it:
#   - creates an unprivileged "wardn" user (no password, no sudo group, not in "docker")
#   - installs the given public key for it
#   - lets it read the systemd journal (the group only gives read access)
#   - with "docker" as third argument, lets it run exactly one command as root,
#     the read-only `docker ps`
#
# Safe to run again. Read it before running it as root.
set -eu

usage="usage: provision-guest.sh <CTID> <public-key-file> [docker]"
ctid=${1:?$usage}
key=${2:?$usage}
mode=${3:-}
rule='wardn ALL=(root) NOPASSWD: /usr/bin/docker ps -a --format {{json .}}'

guest() { pct exec "$ctid" -- "$@"; }

if [ "$mode" = docker ]; then
    guest test -x /usr/bin/docker || { echo "CT $ctid: no /usr/bin/docker" >&2; exit 1; }
    guest sh -c 'command -v sudo >/dev/null' || { echo "CT $ctid: sudo is not installed" >&2; exit 1; }
elif [ -n "$mode" ]; then
    echo "$usage" >&2
    exit 1
fi

guest id wardn >/dev/null 2>&1 || guest adduser --disabled-password --gecos "" wardn
guest install -d -m 700 -o wardn -g wardn /home/wardn/.ssh
pct push "$ctid" "$key" /home/wardn/.ssh/authorized_keys --perms 600
guest chown wardn:wardn /home/wardn/.ssh/authorized_keys
guest sh -c 'getent group systemd-journal >/dev/null && usermod -aG systemd-journal wardn || true'

if [ "$mode" = docker ]; then
    # Check the rule before installing it: a broken sudoers file would lock out sudo.
    guest sh -c "echo '$rule' > /tmp/wardn-docker \
        && visudo -cf /tmp/wardn-docker \
        && install -m 440 -o root -g root /tmp/wardn-docker /etc/sudoers.d/wardn-docker \
        && rm /tmp/wardn-docker"
fi

echo "CT $ctid ready"
