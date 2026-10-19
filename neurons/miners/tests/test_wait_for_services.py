"""wait_for_services_sync must wait out a Postgres switchover, not exit on the first refused connect."""

import pytest
import sqlalchemy
from sqlalchemy.exc import OperationalError

from core import utils


class _Conn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *_a):
        return None


class _Engine:
    def __init__(self, fail):
        self.fail = fail

    def connect(self):
        if self.fail:
            raise OperationalError("connect", {}, Exception("Connection refused"))
        return _Conn()

    def dispose(self):
        pass


@pytest.fixture
def fakes(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _s: None)
    return monkeypatch, []


def test_retries_until_postgres_answers(fakes):
    monkeypatch, calls = fakes

    def create_engine(_url):
        calls.append(1)
        return _Engine(fail=len(calls) < 3)

    monkeypatch.setattr(sqlalchemy, "create_engine", create_engine)
    utils.wait_for_services_sync()
    assert len(calls) == 3


def test_raises_after_timeout(fakes):
    monkeypatch, _ = fakes
    monkeypatch.setattr(sqlalchemy, "create_engine", lambda _url: _Engine(fail=True))
    with pytest.raises(OperationalError):
        utils.wait_for_services_sync(timeout=-1)
