import asyncio
import logging
from typing import Annotated

import bittensor
from clients.miner_client import MinerClient
from datura.requests.miner_requests import AcceptJobRequest, DeclineJobRequest
from fastapi import Depends
from requests.api_requests import MinerRequestPayload

from core.config import settings

logger = logging.getLogger(__name__)


JOB_LENGTH = 300


class MinerService:
    async def request_resource_to_miner(self, payload: MinerRequestPayload):
        loop = asyncio.get_event_loop()
        key: bittensor.Keypair = settings.get_bittensor_wallet().get_hotkey()

        miner_client = MinerClient(
            loop=loop,
            miner_address=payload.miner_address,
            miner_port=payload.miner_port,
            miner_hotkey=payload.miner_hotkey,
            my_hotkey=key.ss58_address,
            keypair=key,
        )

        async with miner_client:
            try:
                msg = await asyncio.wait_for(
                    miner_client.job_state.miner_ready_or_declining_future, JOB_LENGTH
                )
            except TimeoutError:
                msg = None

            if isinstance(msg, DeclineJobRequest) or msg is None:
                logger.info(f"Miner {miner_client.miner_name} won't do job: {msg}")
                return
            elif isinstance(msg, AcceptJobRequest):
                logger.info(f"Miner {miner_client.miner_name} will do job: {msg}")
            else:
                raise ValueError(f"Unexpected msg: {msg}")


MinerServiceDep = Annotated[MinerService, Depends(MinerService)]
