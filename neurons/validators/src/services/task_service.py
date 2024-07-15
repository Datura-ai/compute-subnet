from typing import Annotated

import bittensor
from daos.task import TaskDao
from fastapi import Depends

from core.config import settings
from services.ssh_service import SSHService


class TaskService:
    def __init__(
        self,
        task_dao: Annotated[TaskDao, Depends(TaskDao)],
        ssh_service: Annotated[SSHService, Depends(SSHService)],
    ):
        self.task_dao = task_dao
        self.ssh_service = ssh_service

    def get_decrypted_private_key_for_task(self, uuid: str) -> str | None:
        task = self.task_dao.get_task_by_uuid(uuid)
        if task is None:
            return None
        my_key: bittensor.Keypair = settings.get_bittensor_wallet().get_hotkey()
        return self.ssh_service.decrypt_payload(my_key.ss58_address, task.ssh_private_key)


TaskServiceDep = Annotated[TaskService, Depends(TaskService)]
