import asyncio
import io
from pathlib import Path
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

from paramiko import SSHClient, AutoAddPolicy, Ed25519Key 

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

                await self.connect_ssh(msg.ssh_username, private_key.decode('utf-8'), miner_client)
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

    async def connect_ssh(self, ssh_username: str, private_key: str, miner_client: MinerClient):
        try:
            private_key = self.ssh_service.decrypt_payload(miner_client.keypair.ss58_address, private_key)
            pkey = Ed25519Key.from_private_key(io.StringIO(private_key))

            ssh_client = SSHClient()
            ssh_client.set_missing_host_key_policy(AutoAddPolicy())
            ssh_client.connect(hostname=miner_client.miner_address, username=ssh_username, look_for_keys=False, pkey=pkey)

            localFilePath = str(Path(__file__).parent / ".." / "test.py")
            remoteFilePath = f"/home/{ssh_username}/test.py"

            ftp_client=ssh_client.open_sftp()
            ftp_client.put(localFilePath, remoteFilePath)
            ftp_client.close()

            _, stdout, stderr = ssh_client.exec_command(f"python3 {remoteFilePath}")
            results = stdout.readlines()
            errors = stderr.readlines()
            if (len(errors) > 0):
                raise Exception("Failed to execute command!") 

            logger.info('ssh results ==>', results)

            ssh_client.close()
        except Exception as e:
            logger.error('ssh connection error', e)
            raise e

MinerServiceDep = Annotated[MinerService, Depends(MinerService)]
