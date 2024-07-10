from fastapi import APIRouter
from requests.api_requests import MinerRequestPayload

apis_router = APIRouter()


@apis_router.post("/miner_request")
async def validator_interface(miner_payload: MinerRequestPayload):
    pass
