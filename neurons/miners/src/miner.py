from fastapi import FastAPI

from core.config import settings
from routes.validator_interface import validator_router

app = FastAPI(
    title=settings.PROJECT_NAME,
)

app.include_router(validator_router)
