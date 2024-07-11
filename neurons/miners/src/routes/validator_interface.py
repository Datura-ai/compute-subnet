from typing import Annotated

from fastapi import APIRouter, Depends

from consumers.validator_consumer import ValidatorConsumer

validator_router = APIRouter()


@validator_router.websocket("/validator/{validator_key}")
async def validator_interface(consumer: Annotated[ValidatorConsumer, Depends(ValidatorConsumer)]):
    await consumer.handle()
