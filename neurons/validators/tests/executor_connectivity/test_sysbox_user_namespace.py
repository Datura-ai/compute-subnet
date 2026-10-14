"""G21: hello-world inside the sysbox probe container passes under privileged runc too; only sysbox gives the
container a user namespace, so an identity uid map is logged and, under the flag, refuses sysbox."""

import logging
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from core.config import settings
from services.executor_connectivity import dind_probe
from services.executor_connectivity.dind_probe import DindVerifier

SYSBOX_MAP = "         0     165536      65536\n"
IDENTITY_MAP = "         0          0 4294967295\n"


class _InnerSSH:
    def __init__(self, uid_map: str | None, exit_status: int = 0):
        self.uid_map = uid_map
        self.exit_status = exit_status

    async def run(self, command):
        if command == "cat /proc/self/uid_map":
            return SimpleNamespace(stdout=self.uid_map, stderr="", exit_status=self.exit_status)
        return SimpleNamespace(stdout="Hello from Docker!", stderr="", exit_status=0)


def _identity_logs(caplog) -> list[logging.LogRecord]:
    return [record for record in caplog.records if "SYSBOX_IDENTITY_UID_MAP" in record.getMessage()]


@pytest.mark.asyncio
async def test_a_user_namespace_is_sysbox(caplog):
    with caplog.at_level(logging.WARNING, logger=dind_probe.logger.name):
        works = await DindVerifier(Mock())._sysbox_works(_InnerSSH(SYSBOX_MAP), {})

    assert works is True
    assert _identity_logs(caplog) == []


@pytest.mark.asyncio
async def test_an_identity_map_is_logged_and_still_sysbox_by_default(caplog):
    with caplog.at_level(logging.WARNING, logger=dind_probe.logger.name):
        works = await DindVerifier(Mock())._sysbox_works(_InnerSSH(IDENTITY_MAP), {})

    assert works is True
    assert len(_identity_logs(caplog)) == 1


@pytest.mark.asyncio
async def test_an_identity_map_is_not_sysbox_under_enforcement():
    with patch.object(settings, "SYSBOX_USERNS_ENFORCEMENT_ENABLED", True):
        works = await DindVerifier(Mock())._sysbox_works(_InnerSSH(IDENTITY_MAP), {})

    assert works is False


@pytest.mark.asyncio
@pytest.mark.parametrize("uid_map,exit_status", [("", 0), (None, 0), (IDENTITY_MAP, 1)])
async def test_an_unreadable_map_decides_nothing(uid_map, exit_status):
    with patch.object(settings, "SYSBOX_USERNS_ENFORCEMENT_ENABLED", True):
        works = await DindVerifier(Mock())._sysbox_works(_InnerSSH(uid_map, exit_status), {})

    assert works is True


def test_user_namespace_enforcement_is_off_by_default():
    assert type(settings).model_fields["SYSBOX_USERNS_ENFORCEMENT_ENABLED"].default is False
