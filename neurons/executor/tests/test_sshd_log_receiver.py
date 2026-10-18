import sys
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1])
)  # the receiver sits beside run.sh, not in src/

import sshd_log_receiver  # noqa: E402
from sshd_log_receiver import record  # noqa: E402

ACCEPTED = "<38>Oct 18 06:27:30 sshd[812]: Accepted publickey for root from 203.0.113.9 port 51022 ssh2: ED25519 SHA256:aaa"


def test_a_login_is_kept_with_sshds_envelope(tmp_path):
    log = tmp_path / "sshd-auth.log"

    record(ACCEPTED, str(log))
    record(
        "<38>Oct 18 06:27:31 sshd[813]: Connection closed by 198.51.100.4 port 4022 [preauth]",
        str(log),
    )

    lines = log.read_text().splitlines()
    assert len(lines) == 1 and lines[0].endswith(ACCEPTED)


def test_a_full_log_is_rotated_before_the_next_login(tmp_path, monkeypatch):
    monkeypatch.setattr(sshd_log_receiver, "ROTATE_BYTES", 10)
    log = tmp_path / "sshd-auth.log"
    log.write_text("x" * 20 + "\n")

    record(ACCEPTED, str(log))

    assert (tmp_path / "sshd-auth.log.1").read_text() == "x" * 20 + "\n"
    assert log.read_text().endswith(f"{ACCEPTED}\n")
