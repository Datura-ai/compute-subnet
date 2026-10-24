"""DAH-3240 rental volume fast path: a failed loopback plugin enable never leaks host text into errors or logs."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest
from payload_models.payloads import ContainerCreateRequest
from services.docker_service import (
    DockerService,
    LoopbackPluginDisabledError,
    VolumeHostProbe,
)
from test_deploy_optimizations import (
    _patch_happy,
    _payload as _deploy_payload,
    _run as _run_create_container,
    _ssh_client as _deploy_ssh_client,
)
from test_docker_service import (
    _SIZING_GB,
    _FakeRentalDockerFactory,
)


@pytest.fixture
def docker_service():
    return DockerService(
        ssh_service=Mock(),
        redis_service=Mock(),
        attestation_service=Mock(),
        rental_docker_client_factory=_FakeRentalDockerFactory(),
    )


# ---------------------------------------------------------------------------
# the probe command
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# the parser
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# the command and the parser together, through a real shell and a docker stub
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# probe_volume_host is never fatal
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# resolve_volume_sizing: same numbers, fewer commands
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# create_local_volume: skip the Docker Hub round trip when the plugin is enabled
# ---------------------------------------------------------------------------


async def _create_volume(docker_service, ssh_client, probe):
    docker_service.stream_log = AsyncMock()
    docker_client = docker_service.rental_docker_client_factory.client
    await docker_service.create_local_volume(
        ssh_client=ssh_client,
        docker_client=docker_client,
        local_volume="volume_test",
        log_tag="tag",
        log_text="Creating docker volume volume_test",
        log_extra={},
        limit=40,
        timeout=10,
        host_probe=probe,
    )
    return docker_client.created_volumes


def _disabled_plugin_probe() -> VolumeHostProbe:
    return VolumeHostProbe(
        docker_root_dir="/var/lib/docker",
        df_avail_bytes=None,
        vloopback_volume_names=[],
        loopback_plugin_enabled=False,
        loopback_plugin_installed=True,
    )


@pytest.mark.asyncio
async def test_create_local_volume_disabled_plugin_enable_error_logs_only_its_type(
    docker_service, monkeypatch
):
    from services import docker_service as docker_service_module

    secret_text = "ssh ubuntu@10.1.2.3: token=ghp_leakme at /home/provider/.ssh/id_rsa"
    ssh_client = Mock()
    ssh_client.run = AsyncMock(side_effect=OSError(secret_text))
    warning = Mock()
    monkeypatch.setattr(docker_service_module.logger, "warning", warning)

    with pytest.raises(LoopbackPluginDisabledError) as exc_info:
        await _create_volume(docker_service, ssh_client, _disabled_plugin_probe())

    assert "enable error: OSError" in str(exc_info.value)
    assert secret_text not in str(exc_info.value)
    (logged,), _ = warning.call_args
    assert str(logged).startswith("Loopback plugin enable failed")
    assert logged.extra["error_type"] == "OSError"
    assert logged.extra["loopback_plugin"] == "vloopback"
    assert "error" not in logged.extra
    full = logged.to_full_string()
    assert "10.1.2.3" not in full and "ghp_leakme" not in full and "id_rsa" not in full


# ---------------------------------------------------------------------------
# create_container: the flag decides whether the probe runs at all
# ---------------------------------------------------------------------------


@pytest.fixture
def svc():
    return DockerService(ssh_service=Mock(), redis_service=Mock(), attestation_service=Mock())


def _create_payload() -> ContainerCreateRequest:
    return _deploy_payload(disk_share=0.5, storage_limit_gb=1, volume_limit_gb=2)


@pytest.mark.asyncio
async def test_create_container_probes_once_and_passes_it_on(svc, monkeypatch):
    ssh_client = _deploy_ssh_client()
    _patch_happy(svc, monkeypatch, ssh_client)
    probe = VolumeHostProbe(
        docker_root_dir="/var/lib/docker",
        df_avail_bytes=900 * _SIZING_GB,
        vloopback_volume_names=[],
        loopback_plugin_enabled=True,
    )
    monkeypatch.setattr(svc, "probe_volume_host", AsyncMock(return_value=probe))

    await _run_create_container(svc, _create_payload())

    svc.probe_volume_host.assert_awaited_once()
    assert svc.probe_volume_host.await_args.kwargs["with_df"] is True
    assert svc.resolve_volume_sizing.await_args.kwargs["host_probe"] is probe
    assert svc.create_local_volume.await_args.kwargs["host_probe"] is probe


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        dict(disk_share=None, storage_limit_gb=1, volume_limit_gb=2),  # legacy passthrough
        dict(storage_limit_gb=None, volume_limit_gb=2),  # storage_opt_unsupported passthrough
    ],
)
async def test_create_container_passthrough_sizing_with_a_limit_probes_without_df(
    svc, monkeypatch, overrides
):
    # Sizing is a passthrough (no df to measure) but the volume has a size, so the create still
    # needs the root dir and the plugin state: probe, without the helper container.
    ssh_client = _deploy_ssh_client()
    _patch_happy(svc, monkeypatch, ssh_client)
    monkeypatch.setattr(svc, "probe_volume_host", AsyncMock(return_value=None))

    await _run_create_container(svc, _deploy_payload(**overrides))

    assert svc.probe_volume_host.await_args.kwargs["with_df"] is False
    assert svc.create_local_volume.await_args.kwargs["host_probe"] is None


@pytest.mark.asyncio
async def test_create_container_unlimited_volume_on_passthrough_never_probes(
    svc, monkeypatch
):
    # No df to measure (passthrough) and no `-o size=` volume (no plugin, no root dir needed):
    # nothing would read the probe, so it is not run.
    ssh_client = _deploy_ssh_client()
    _patch_happy(svc, monkeypatch, ssh_client)
    monkeypatch.setattr(svc, "probe_volume_host", AsyncMock())

    await _run_create_container(svc, _deploy_payload(storage_limit_gb=None, volume_limit_gb=None))

    svc.probe_volume_host.assert_not_awaited()
    assert svc.create_local_volume.await_args.kwargs["host_probe"] is None


@pytest.mark.asyncio
async def test_create_container_probe_failure_takes_the_per_command_path(svc, monkeypatch):
    ssh_client = _deploy_ssh_client()
    _patch_happy(svc, monkeypatch, ssh_client)
    monkeypatch.setattr(svc, "probe_volume_host", AsyncMock(return_value=None))

    result = await _run_create_container(svc, _create_payload())

    assert result.__class__.__name__ == "ContainerCreated"
    assert svc.resolve_volume_sizing.await_args.kwargs["host_probe"] is None
    assert svc.create_local_volume.await_args.kwargs["host_probe"] is None
