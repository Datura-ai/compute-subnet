from fastapi import APIRouter
from requests.api_requests import MinerRequestPayload
from services.miner_service import MinerServiceDep

apis_router = APIRouter()


@apis_router.post("/miner_request")
async def request_resource_to_miner(
    miner_payload: MinerRequestPayload, miner_service: MinerServiceDep
):
    """Requesting resource to miner."""
    await miner_service.request_resource_to_miner(miner_payload)
