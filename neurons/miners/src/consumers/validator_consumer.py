import logging
import time

import bittensor
from datura.consumers.base import BaseConsumer
from datura.requests.miner_requests import AcceptJobRequest, GenericError
from datura.requests.validator_requests import AuthenticateRequest, BaseValidatorRequest
from fastapi import WebSocket

from core.config import settings

AUTH_MESSAGE_MAX_AGE = 10
MAX_MESSAGE_COUNT = 10

logger = logging.getLogger(__name__)


class ValidatorConsumer(BaseConsumer):
    def __init__(self, websocket: WebSocket, validator_key: str):
        super().__init__(websocket)
        self.validator_key = validator_key
        self.my_hotkey = settings.get_bittensor_wallet().get_hotkey().ss58_address
        self.validator_authenticated = False
        self.msg_queue = []

    def accepted_request_type(self):
        return BaseValidatorRequest

    def verify_auth_msg(self, msg: AuthenticateRequest) -> tuple[bool, str]:
        if msg.payload.timestamp < time.time() - AUTH_MESSAGE_MAX_AGE:
            return False, "msg too old"
        if msg.payload.miner_hotkey != self.my_hotkey:
            return False, f"wrong miner hotkey ({self.my_hotkey}!={msg.payload.miner_hotkey})"
        if msg.payload.validator_hotkey != self.validator_key:
            return (
                False,
                f"wrong validator hotkey ({self.validator_key}!={msg.payload.validator_hotkey})",
            )

        keypair = bittensor.Keypair(ss58_address=self.validator_key)
        if keypair.verify(msg.blob_for_signing(), msg.signature):
            return True, ""

    async def handle_authentication(self, msg: AuthenticateRequest):
        authenticated, error_msg = self.verify_auth_msg(msg)
        if not authenticated:
            response_msg = f"Validator {self.validator_key} not authenticated due to: {error_msg}"
            logger.info(response_msg)
            await self.send_message(GenericError(details=response_msg))
            await self.disconnect()
            return

        self.validator_authenticated = True
        for msg in self.msg_queue:
            await self.handle_message(msg)

    async def handle_message(self, msg: BaseValidatorRequest):
        if isinstance(msg, AuthenticateRequest):
            await self.handle_authentication(msg)

            # TODO: update logic here, fow now, it sends acceptjobrequest regardless
            if self.validator_authenticated:
                await self.send_message(AcceptJobRequest())

            return

        if not self.validator_authenticated:
            if len(self.msg_queue) <= MAX_MESSAGE_COUNT:
                self.msg_queue.append(msg)
            return
