import uuid
from uuid import UUID

from sqlmodel import Field, SQLModel


class Validator(SQLModel, table=True):
    """Task model."""

    uuid: UUID | None = Field(default_factory=uuid.uuid4, primary_key=True)
    validator_hotkey: str = Field(unique=True)
    active: bool
