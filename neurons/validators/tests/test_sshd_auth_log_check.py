from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.task.checks.sshd_auth_log import READ_AND_EMPTY, SshdAuthLogCheck
from services.task.messages import SshdAuthLogMessages as Msg

LINE = "Oct 18 06:27:30 sshd[812]: Accepted publickey for root from 203.0.113.9 port 51022 ssh2: ED25519 SHA256:abc"


def _ssh(stdout="", exit_status=0, error=None):
    ssh = AsyncMock()
    ssh.run.side_effect = error or None
    ssh.run.return_value = SimpleNamespace(stdout=stdout, exit_status=exit_status)
    return ssh


@pytest.mark.asyncio
async def test_logins_are_logged_and_the_log_is_emptied(context_factory, caplog):
    ssh = _ssh(stdout=f"{LINE}\n\n")
    with caplog.at_level("INFO"):
        result = await SshdAuthLogCheck().run(context_factory(ssh=ssh))
    assert result.passed is True
    assert result.event.reason_code == Msg.LOGINS.reason
    assert result.event.what_we_saw["login_count"] == 1
    assert ': > "$f"' in ssh.run.call_args.args[0] == READ_AND_EMPTY
    logged = [
        r.msg.extra
        for r in caplog.records
        if getattr(r.msg, "message", None) == "Executor sshd logins"
    ]
    assert [extra["logins"] for extra in logged] == [[LINE]]


@pytest.mark.asyncio
async def test_no_logins_logs_nothing(context_factory, caplog):
    with caplog.at_level("INFO"):
        result = await SshdAuthLogCheck().run(context_factory(ssh=_ssh()))
    assert result.event.what_we_saw["login_count"] == 0
    assert not any("Executor sshd logins" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_an_executor_without_the_log_passes_unread(context_factory):
    result = await SshdAuthLogCheck().run(context_factory(ssh=_ssh(exit_status=3)))
    assert result.passed is True
    assert result.event.reason_code == Msg.UNREAD.reason


@pytest.mark.asyncio
async def test_an_ssh_error_never_fails_the_node(context_factory):
    result = await SshdAuthLogCheck().run(context_factory(ssh=_ssh(error=OSError("closed"))))
    assert result.passed is True
    assert result.event.reason_code == Msg.UNREAD.reason
