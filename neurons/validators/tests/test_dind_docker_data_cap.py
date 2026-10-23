"""A sysbox pod's /var/lib/docker is a capped, sparse vloopback volume, not an uncapped host volume."""

from payload_models.payloads import ContainerCreateRequest, CustomOptions
from services.docker_service import _parse_volume_host_probe
from services.rental_docker_sdk import GpuDockerConfig, _build_host_config_kwargs

from tests.test_deploy_optimizations import svc  # noqa: F401  (pytest fixture)
from tests.test_volume_fast_path import _probe_stdout


def _run_spec(docker_service, docker_data_volume_gb):
    payload = ContainerCreateRequest(
        miner_hotkey="hk",
        executor_id="ex",
        pod_id="pod",
        docker_image="img:dind",
        gpu_uuids=["g0"],
        is_sysbox=True,
    )
    return docker_service._build_rental_container_run_spec(
        payload=payload,
        container_name="pod_test",
        custom_options=CustomOptions(),
        port_maps=[(22, 30022, 40022)],
        local_volume="volume_pod",
        local_volume_path="/root",
        encrypted_local_volume=False,
        external_volume_name=None,
        gpu_devices=GpuDockerConfig(),
        effective_storage_limit_gb=40,
        cpu_count=None,
        docker_data_volume_gb=docker_data_volume_gb,
    )


def test_sysbox_pod_mounts_a_capped_sparse_loopback_volume_at_var_lib_docker(svc):  # noqa: F811
    kwargs = _build_host_config_kwargs(_run_spec(svc, 40))

    [mount] = kwargs["mounts"]
    assert (
        mount["Target"] == "/var/lib/docker" and mount["Type"] == "volume" and not mount["Source"]
    )
    assert mount["VolumeOptions"]["DriverConfig"] == {
        "Name": "vloopback",
        "Options": {"size": "40g", "sparse": "true"},
    }


def test_without_a_docker_data_limit_no_mount_is_sent(svc):  # noqa: F811
    assert "mounts" not in _build_host_config_kwargs(_run_spec(svc, None))


def test_sizing_ignores_anonymous_vloopback_volumes():
    stdout = _probe_stdout(volumes=f"VOL\tvolume_abc\tvloopback\nVOL\t{'a1' * 32}\tvloopback\n")

    assert _parse_volume_host_probe(stdout, with_df=True).vloopback_volume_names == ["volume_abc"]
