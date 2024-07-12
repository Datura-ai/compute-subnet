import asyncio
import logging
from typing import Annotated

import bittensor
from clients.miner_client import MinerClient
from daos.task import TaskDao
from datura.requests.miner_requests import (
    AcceptSSHKeyRequest,
    FailedRequest,
)
from datura.requests.validator_requests import SSHPubKeySubmitRequest
from fastapi import Depends
from models.task import Task, TaskStatus
from requests.api_requests import MinerRequestPayload

from core.config import settings
from services.ssh_service import SSHService

logger = logging.getLogger(__name__)


JOB_LENGTH = 300


class MinerService:
    def __init__(
        self,
        ssh_service: Annotated[SSHService, Depends(SSHService)],
        task_dao: Annotated[TaskDao, Depends(TaskDao)],
    ):
        self.ssh_service = ssh_service
        self.task_dao = task_dao

    async def request_resource_to_miner(self, payload: MinerRequestPayload):
        loop = asyncio.get_event_loop()
        my_key: bittensor.Keypair = settings.get_bittensor_wallet().get_hotkey()

        miner_client = MinerClient(
            loop=loop,
            miner_address=payload.miner_address,
            miner_port=payload.miner_port,
            miner_hotkey=payload.miner_hotkey,
            my_hotkey=my_key.ss58_address,
            keypair=my_key,
        )

        async with miner_client:
            # try:
            #     msg = await asyncio.wait_for(
            #         miner_client.job_state.miner_ready_or_declining_future, JOB_LENGTH
            #     )
            # except TimeoutError:
            #     msg = None
            #
            # if isinstance(msg, DeclineJobRequest) or msg is None:
            #     logger.info(f"Miner {miner_client.miner_name} won't do job: {msg}")
            #     return
            # elif isinstance(msg, AcceptJobRequest):
            #     logger.info(f"Miner {miner_client.miner_name} will do job: {msg}")
            # else:
            #     raise ValueError(f"Unexpected msg: {msg}")

            # generate ssh key and send it to miner
            private_key, public_key = self.ssh_service.generate_ssh_key(my_key.ss58_address)
            await miner_client.send_model(SSHPubKeySubmitRequest(public_key=public_key))

            logger.info("Sent SSH key to miner %s", miner_client.miner_name)

            try:
                msg = await asyncio.wait_for(
                    miner_client.job_state.miner_accepted_ssh_key_or_failed_future, JOB_LENGTH
                )
            except TimeoutError:
                msg = None

            if isinstance(msg, AcceptSSHKeyRequest):
                logger.info(f"Miner {miner_client.miner_name} accepted SSH key: {msg}")
            elif isinstance(msg, FailedRequest):
                logger.info(f"Miner {miner_client.miner_name} failed job: {msg}")
                return
            else:
                raise ValueError(f"Unexpected msg: {msg}")

            self.task_dao.save(
                Task(
                    task_status=TaskStatus.SSHConnected,
                    miner_hotkey=payload.miner_hotkey,
                    ssh_private_key=private_key.decode(),
                )
            )


MinerServiceDep = Annotated[MinerService, Depends(MinerService)]
