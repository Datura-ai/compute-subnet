from datura.consumers.base import BaseConsumer
from datura.requests.base import BaseRequest


class CustomConsumer(BaseConsumer):
    async def handle_message(self, message: BaseRequest):
        pass
