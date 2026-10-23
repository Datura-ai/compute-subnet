"""DAH-5037: a default-job (filler) container that exits at start carries its last log lines."""

from unittest.mock import AsyncMock, Mock

import pytest

from services.docker_service import (
    CONTAINER_LOG_TAIL_MARKER,
    ImageExitedDuringKeyInjection,
    _explain_add_public_keys_failure,
)
from services.rental_docker_sdk import ContainerStateSnapshot

EXITED = ContainerStateSnapshot(
    status="exited", running=False, restarting=False, exit_code=1, restart_count=0, error=None, oom_killed=False
)


def _client(log_tail=None, log_error=None):
    client = Mock()
    client.inspect_container_state = AsyncMock(return_value=EXITED)
    client.container_log_tail = AsyncMock(return_value=log_tail, side_effect=log_error)
    return client


async def _explain(client, capture_log_tail):
    return await _explain_add_public_keys_failure(
        client,
        container_name="c1",
        image="jetskipool/jetski-miner:latest",
        cause=RuntimeError("exec failed"),
        log_extra={},
        capture_log_tail=capture_log_tail,
    )


@pytest.mark.asyncio
async def test_filler_exit_at_start_appends_the_log_tail():
    explained = await _explain(_client(log_tail="error: missing --pool\n"), capture_log_tail=True)

    assert isinstance(explained, ImageExitedDuringKeyInjection)
    assert "exited right after start (exit_code=1)" in str(explained)
    assert str(explained).endswith(f"{CONTAINER_LOG_TAIL_MARKER}error: missing --pool")


@pytest.mark.asyncio
async def test_renter_exit_at_start_never_reads_the_log():
    client = _client(log_tail="renter secret output")

    explained = await _explain(client, capture_log_tail=False)

    assert CONTAINER_LOG_TAIL_MARKER not in str(explained)
    client.container_log_tail.assert_not_called()


@pytest.mark.asyncio
async def test_unreadable_log_keeps_the_exit_message():
    explained = await _explain(_client(log_error=RuntimeError("logs 500")), capture_log_tail=True)

    assert isinstance(explained, ImageExitedDuringKeyInjection)
    assert CONTAINER_LOG_TAIL_MARKER not in str(explained)
