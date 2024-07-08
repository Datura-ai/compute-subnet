import abc
import json

from fastapi import WebSocket, WebSocketDisconnect

from ..requests.base import BaseRequest


class BaseConsumer(abc.ABC):
    def __init__(self, websocket: WebSocket):
        self.websocket = websocket

    async def connect(self):
        await self.websocket.accept()

    async def receive_message(self) -> BaseRequest:
        data = await self.websocket.receive_text()
        return BaseRequest.parse(data)

    async def send_message(self, msg: BaseRequest):
        await self.websocket.send_text(json.dumps(msg.json()))

    async def disconnect(self):
        await self.websocket.close()

    @abc.abstractmethod
    async def handle_message(self, data: BaseRequest):
        raise NotImplementedError

    async def handle(self):
        await self.connect()
        try:
            while True:
                data: BaseRequest = await self.receive_message()
                await self.handle_message(data)
        except WebSocketDisconnect:
            await self.disconnect()
