import asyncio
from typing import Annotated

import bittensor
from clients.miner_client import MinerClient
from fastapi import Depends
from requests.api_requests import MinerRequestPayload

from core.config import settings


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
            # TODO: implement logic of generating ssh key
            # TODO: sending generated pub key to miner
            pass


MinerServiceDep = Annotated[MinerService, Depends(MinerService)]
