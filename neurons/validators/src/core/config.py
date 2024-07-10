from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict()
    PROJECT_NAME: str = "compute-subnet-validator"


settings = Settings()
