import enum

from datura.requests.base import BaseRequest


class RequestType(enum.Enum):
    GenericError = "GenericError"
    AcceptJobRequest = "AcceptJobRequest"
    DeclineJobRequest = "DeclineJobRequest"


class BaseMinerRequest(BaseRequest):
    message_type: RequestType


class GenericError(BaseMinerRequest):
    message_type: RequestType = RequestType.GenericError
    details: str | None = None


class AcceptJobRequest(BaseMinerRequest):
    message_type: RequestType = RequestType.AcceptJobRequest


class DeclineJobRequest(BaseMinerRequest):
    message_type: RequestType = RequestType.DeclineJobRequest
