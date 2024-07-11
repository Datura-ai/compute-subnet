import logging

from fastapi import FastAPI

from core.config import settings
from routes.apis import apis_router

# Set up logging
logging.basicConfig(level=logging.INFO)

app = FastAPI(
    title=settings.PROJECT_NAME,
)

app.include_router(apis_router)
