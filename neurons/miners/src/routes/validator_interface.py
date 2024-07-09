from typing import Annotated

from fastapi import APIRouter, Depends, WebSocket

from consumers.validator_consumer import ValidatorConsumer

validator_router = APIRouter()


def get_consumer(websocket: WebSocket, validator_id: str) -> ValidatorConsumer:
    return ValidatorConsumer(websocket, validator_id)


@validator_router.websocket("/validator/{validator_id}")
async def validator_interface(consumer: Annotated[ValidatorConsumer, Depends(get_consumer)]):
    await consumer.handle()
