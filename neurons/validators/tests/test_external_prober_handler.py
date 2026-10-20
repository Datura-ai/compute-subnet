"""The outside prober answers no verdict (503) when its own egress is down, never an all-blocked 200 (DAH-4537)."""

import importlib.util
import json
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "external_prober_handler", Path(__file__).parents[1] / "external_prober" / "handler.py"
)
handler = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(handler)

EVENT = {
    "headers": {"Authorization": "Bearer t0k"},
    "body": json.dumps({"targets": [{"host": "1.2.3.4", "port": 8080}]}),
}


@pytest.fixture(autouse=True)
def probe_token_env(monkeypatch):
    monkeypatch.setenv("PROBE_TOKEN", "t0k")


def test_egress_down_answers_no_verdict(monkeypatch):
    monkeypatch.setattr(handler, "_get", lambda url, timeout: None)
    assert handler.handler(EVENT, None)["statusCode"] == 503


def test_canary_ok_reports_each_target(monkeypatch):
    monkeypatch.setattr(handler, "_get", lambda url, timeout: "ok" if url == handler.CANARY_URL else None)
    res = handler.handler(EVENT, None)
    assert res["statusCode"] == 200
    assert json.loads(res["body"])["results"] == [{"host": "1.2.3.4", "port": 8080, "body": None}]
