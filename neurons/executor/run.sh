#!/bin/sh
set -eux -o pipefail

# disk reserve: validator-cycle writes must survive a 100%-full host disk
bash setup_disk_reserve.sh || echo "[disk-reserve] setup failed; continuing without reserve"
bash setup_disk_reserve.sh janitor &
# re-enter cwd through the overlay mount (the old dentry would bypass it)
cd /root/app

# ensure docker group matches host socket
SOCK=/var/run/docker.sock
if [ -S "$SOCK" ]; then
  SGID=$(stat -c %g "$SOCK")
  GNAME=$(getent group "$SGID" | cut -d: -f1 || true)
  if [ -z "$GNAME" ]; then
    groupadd -o -g "$SGID" dockersock
    GNAME=dockersock
  fi
  usermod -aG "$GNAME" liumuser || true
fi

# start ssh service
ssh-keygen -A
# DAH-4629: sshd logs (LogLevel VERBOSE, sshd_setup.sh) to a file instead of a syslog this container
# does not run; the validator reads and empties it each cycle. Emptied here past 50 MB in case no
# validator comes by (a scanner on the ssh port writes a few lines per attempt).
SSHD_AUTH_LOG=/var/log/sshd-auth.log
mkdir -p /run/sshd
/usr/sbin/sshd -E "$SSHD_AUTH_LOG"
(while sleep 600; do [ "$(stat -c %s "$SSHD_AUTH_LOG" 2>/dev/null || echo 0)" -gt 52428800 ] && : >"$SSHD_AUTH_LOG"; done) &

# run fastapi app
pdm run python src/executor.py
