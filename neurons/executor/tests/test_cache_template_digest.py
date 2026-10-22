"""Digest-pinned cache template refresh (DAH-2461).

The backend serves the expected Docker Hub digest for the default cache template;
``_ensure_template`` must treat it as the source of truth (pull by digest + retag on
mismatch) and only fall back to the daemon's remote-digest compare when the backend
sends no digest — the daemon path resolves through provider registry mirrors, which
can serve stale manifests for a tag.
"""

import asyncio
from unittest.mock import MagicMock

import docker
import pytest

# conftest replaces the docker module with a MagicMock; except-clauses in the
# service need a real exception class to catch.
docker.errors.ImageNotFound = type("ImageNotFound", (Exception,), {})

from services import cache_template_service  # noqa: E402

EXPECTED_DIGEST = "sha256:70bd5fa697877594b753a146e207ca4de66d9b875d606ae09e6ee7bac8f4f423"
STALE_DIGEST = "sha256:2d19c94ce8a37c6fa364f8a6211d8b6dc1a44ece574c4c22ab5579925ce7a4c8"
REPO = "daturaai/pytorch"
TAG = "2.12.0-py3.12-cuda13.0.2-devel-ubuntu24.04-dind"


def _make_client(local_digests: list[str] | None = None, image_absent: bool = False) -> MagicMock:
    client = MagicMock()
    local = MagicMock()
    local.attrs = {"RepoDigests": [f"{REPO}@{digest}" for digest in (local_digests or [])]}
    # What the lookup after a pull returns; `image_absent` is about the tag before the pull.
    client.pulled_image = MagicMock()

    def get(ref):
        if "@" in ref:
            return client.pulled_image
        if image_absent:
            raise docker.errors.ImageNotFound("absent")
        return local

    client.images.get.side_effect = get
    client.api.pull.side_effect = lambda *args, **kwargs: iter([{"status": "Pull complete"}])
    client.images.list.return_value = []
    return client


def _template(digest: str | None = None) -> dict:
    data = {
        "docker_image": REPO,
        "docker_image_tag": TAG,
        "docker_image_size": 0,
    }
    if digest is not None:
        data["docker_image_digest"] = digest
    return data


def test_backend_digest_match_skips_pull():
    # Arrange
    client = _make_client(local_digests=[EXPECTED_DIGEST])

    # Act
    asyncio.run(cache_template_service._ensure_template(client, _template(EXPECTED_DIGEST)))

    # Assert
    client.api.pull.assert_not_called()
    client.images.get_registry_data.assert_not_called()


def test_backend_digest_mismatch_pulls_pinned_and_retags():
    # Arrange: local tag poisoned by a stale mirror, backend knows the fresh digest
    client = _make_client(local_digests=[STALE_DIGEST])

    # Act
    asyncio.run(cache_template_service._ensure_template(client, _template(EXPECTED_DIGEST)))

    # Assert: content-addressed pull, then the tag is re-pointed at the pinned build
    client.api.pull.assert_called_once_with(REPO, tag=EXPECTED_DIGEST, stream=True, decode=True)
    client.pulled_image.tag.assert_called_once_with(REPO, TAG)
    client.images.get_registry_data.assert_not_called()


def test_backend_digest_image_absent_pulls_pinned():
    # Arrange: nothing cached locally yet
    client = _make_client(image_absent=True)

    # Act
    asyncio.run(cache_template_service._ensure_template(client, _template(EXPECTED_DIGEST)))

    # Assert: first pull is digest-pinned too (bypasses the mirror from the start)
    client.api.pull.assert_called_once_with(REPO, tag=EXPECTED_DIGEST, stream=True, decode=True)
    client.pulled_image.tag.assert_called_once_with(REPO, TAG)


def test_no_backend_digest_matching_remote_skips_pull():
    # Arrange: legacy path — daemon reports the same digest as cached
    client = _make_client(local_digests=[STALE_DIGEST])
    registry_data = MagicMock()
    registry_data.id = STALE_DIGEST
    client.images.get_registry_data.return_value = registry_data

    # Act
    asyncio.run(cache_template_service._ensure_template(client, _template()))

    # Assert
    client.api.pull.assert_not_called()


def test_no_backend_digest_changed_remote_pulls_by_tag():
    # Arrange: legacy path — daemon reports a new digest
    client = _make_client(local_digests=[STALE_DIGEST])
    registry_data = MagicMock()
    registry_data.id = EXPECTED_DIGEST
    client.images.get_registry_data.return_value = registry_data

    # Act
    asyncio.run(cache_template_service._ensure_template(client, _template()))

    # Assert: legacy pull by repo + tag, no digest pinning involved
    client.api.pull.assert_called_once_with(REPO, tag=TAG, stream=True, decode=True)


def test_no_backend_digest_unreadable_remote_keeps_local():
    # Arrange: legacy path — daemon cannot resolve the remote digest
    client = _make_client(local_digests=[STALE_DIGEST])
    client.images.get_registry_data.side_effect = RuntimeError("registry unreachable")

    # Act
    asyncio.run(cache_template_service._ensure_template(client, _template()))

    # Assert
    client.api.pull.assert_not_called()


# Low-disk prune: every image no container uses goes, except the templates this loop keeps warm.
def _image(image_id: str, tags: list[str]) -> MagicMock:
    image = MagicMock()
    image.id, image.tags = image_id, tags
    return image


def _prune_client(images: list[MagicMock], container_image_ids: list[str]) -> MagicMock:
    client = MagicMock()
    client.info.return_value = {}
    client.images.list.return_value = images
    client.containers.list.return_value = [
        MagicMock(attrs={"Image": image_id}, status="running") for image_id in container_image_ids
    ]
    return client


def _disk(monkeypatch, free_gb: float, percent: float) -> None:
    usage = MagicMock(free=int(free_gb * 1024**3), percent=percent)
    monkeypatch.setattr(cache_template_service.psutil, "disk_usage", lambda path: usage)
    monkeypatch.setattr(cache_template_service, "rental_activity", lambda client: None)


def test_low_disk_removes_unused_images_and_keeps_used_and_template_ones(monkeypatch):
    _disk(monkeypatch, free_gb=30, percent=97.8)
    images = [
        _image("sha256:own", ["daturaai/compute-subnet-executor:latest"]),
        _image("sha256:tpl", [f"{REPO}:{TAG}"]),
        _image("sha256:old", ["someone/old:1", "someone/old:latest"]),
        _image("sha256:dangling", []),
    ]
    client = _prune_client(images, ["sha256:own"])
    assert cache_template_service._prune_unused_images_when_disk_low(client, {f"{REPO}:{TAG}"}) == 2
    removed = [call.args[0] for call in client.images.remove.call_args_list]
    assert removed == ["someone/old:1", "someone/old:latest", "sha256:dangling"]


def test_a_disk_over_the_listing_floor_removes_nothing(monkeypatch):
    _disk(monkeypatch, free_gb=500, percent=50)
    client = _prune_client([_image("sha256:old", ["someone/old:1"])], [])
    assert cache_template_service._prune_unused_images_when_disk_low(client, set()) == 0
    client.images.remove.assert_not_called()


def test_a_low_docker_data_root_prunes_even_when_root_has_room(monkeypatch, tmp_path):
    _disk(monkeypatch, free_gb=500, percent=50)
    low = MagicMock(free=30 * 1024**3, percent=97)
    roomy = cache_template_service.psutil.disk_usage
    monkeypatch.setattr(
        cache_template_service.psutil, "disk_usage", lambda path: low if path == str(tmp_path) else roomy(path)
    )
    client = _prune_client([_image("sha256:old", ["someone/old:1"])], [])
    client.info.return_value = {"DockerRootDir": str(tmp_path)}
    assert cache_template_service._prune_unused_images_when_disk_low(client, set()) == 1


@pytest.mark.parametrize("busy_after_first_removal", [False, True])
def test_a_busy_node_keeps_its_images(monkeypatch, busy_after_first_removal):
    _disk(monkeypatch, free_gb=30, percent=97.8)
    client = _prune_client([_image("old", ["old:1"]), _image("rental", ["customer/app:1"])], [])
    # a rental can start mid-sweep, after it found its image locally and skipped the pull
    monkeypatch.setattr(
        cache_template_service,
        "rental_activity",
        lambda _: "docker-over-ssh session" if client.images.remove.called or not busy_after_first_removal else None,
    )
    expected = ["old:1"] if busy_after_first_removal else []
    assert cache_template_service._prune_unused_images_when_disk_low(client, set()) == len(expected)
    assert [call.args[0] for call in client.images.remove.call_args_list] == expected
