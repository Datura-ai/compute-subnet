from datura.consumers.base import BaseConsumer
from datura.requests.base import BaseRequest
from fastapi import WebSocket


class ValidatorConsumer(BaseConsumer):
    def __init__(self, websocket: WebSocket, validator_id: str):
        super().__init__(websocket)
        self.validator_id = validator_id

    async def handle_message(self, message: BaseRequest):
        pass
