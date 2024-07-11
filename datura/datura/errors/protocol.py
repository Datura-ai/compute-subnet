from datura.requests.base import BaseRequest


class UnsupportedMessageReceived(Exception):
    def __init__(self, msg: BaseRequest):
        self.msg = msg

    def __str__(self):
        return f"{type(self).__name__}: {self.msg.json()}"

    __repr__ = __str__
