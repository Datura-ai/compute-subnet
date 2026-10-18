"""The container's /dev/log for sshd: it runs no syslog daemon, so sshd's lines would be dropped.

Keeps the `Accepted ...` login lines with sshd's own envelope (time and `sshd[pid]`), prefixed by the
time received in UTC, in SSHD_AUTH_LOG for the validator to read (it keeps its own cursor there)."""

import os
import socket
from datetime import UTC, datetime

DEV_LOG = "/dev/log"
SSHD_AUTH_LOG = "/var/log/sshd-auth.log"
# past this the file is renamed to .1; the new file is a new inode, which restarts the validator's cursor.
# shortcut: lines the validator had not read in .1 are not read, only after 50 MB of logins unread; a cursor over .1 then
ROTATE_BYTES = 50 * 1024 * 1024


def record(message: str, log: str = SSHD_AUTH_LOG) -> None:
    line = " ".join(message.split())
    if "Accepted " not in line:
        return
    if os.path.exists(log) and os.path.getsize(log) > ROTATE_BYTES:
        os.replace(log, f"{log}.1")
    with open(log, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now(UTC).isoformat(timespec='seconds')} {line}\n")


def main() -> None:
    if os.path.exists(DEV_LOG):
        os.unlink(DEV_LOG)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sock.bind(DEV_LOG)
    os.chmod(DEV_LOG, 0o666)
    while True:
        record(sock.recv(16384).decode("utf-8", errors="replace"))


if __name__ == "__main__":
    main()
