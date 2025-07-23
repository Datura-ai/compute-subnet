import abc
import logging

from fastapi import WebSocket, WebSocketDisconnect

from ..requests.base import BaseRequest

logger = logging.getLogger(__name__)


class BaseConsumer(abc.ABC):
    def __init__(self, websocket: WebSocket):
        self.websocket = websocket
        self.disconnected = True

    @abc.abstractmethod
    def accepted_request_type(self) -> type[BaseRequest]:
        pass

    async def connect(self):
        await self.websocket.accept()
        self.disconnected = False

    async def receive_message(self) -> BaseRequest:
        data = await self.websocket.receive_text()
        return self.accepted_request_type().parse(data)

    async def send_message(self, msg: BaseRequest):
        await self.websocket.send_text(msg.model_dump_json())

    async def disconnect(self):
        self.disconnected = True
        try:
            await self.websocket.close()
        except Exception:
            pass

    @abc.abstractmethod
    async def handle_message(self, data: BaseRequest):
        raise NotImplementedError

    async def handle(self):
        # await self.connect()
        try:
            while not self.disconnected:
                data: BaseRequest = await self.receive_message()
                await self.handle_message(data)
        except WebSocketDisconnect as ex:
            if ex.code != 1000 or len(ex.reason) > 0:
              logger.info("Websocket connection closed, e: %s", str(ex))
            await self.disconnect()
        except Exception as ex:
            logger.info("Handling message error: %s", str(ex))
            await self.disconnect()
