from pydantic import BaseModel


class MinerRequestPayload(BaseModel):
    miner_hotkey: str
    miner_address: str
    miner_port: int
