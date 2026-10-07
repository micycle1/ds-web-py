from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def fixture_json(name: str) -> Any:
    return json.loads(fixture_text(name))


class FakeResponse:
    def __init__(self, text: str = "", content_type: str = "text/html", status_code: int = 200):
        self.text = text
        self.content = text.encode("utf-8")
        self.headers = {"Content-Type": content_type}
        self.status_code = status_code

    def json(self) -> Any:
        return json.loads(self.text)


class FakeSession:
    """Stands in for ds_web._session.Session: answers each (method, endpoint) from a
    table of handlers and records every call, so client logic can be tested offline."""

    username = "TESTUSER"

    def __init__(self, routes: dict[str, Any]):
        self.routes = routes
        self.calls: list[dict[str, Any]] = []

    def ensure_login(self) -> None:
        pass

    def request(self, method: str, endpoint: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"method": method, "endpoint": endpoint, **kwargs})
        handler = self.routes[endpoint]
        result = handler(**kwargs) if callable(handler) else handler
        return result if isinstance(result, FakeResponse) else FakeResponse(result)

    def json(self, method: str, endpoint: str, **kwargs: Any) -> Any:
        self.calls.append({"method": method, "endpoint": endpoint, **kwargs})
        handler = self.routes[endpoint]
        result = handler(**kwargs) if callable(handler) else handler
        return json.loads(result) if isinstance(result, str) else result

    def close(self) -> None:
        pass


@pytest.fixture
def make_client():
    from ds_web import DatastreamWebClient

    def build(routes: dict[str, Any], **kwargs: Any) -> tuple[DatastreamWebClient, FakeSession]:
        client = DatastreamWebClient("TESTUSER", "secret", **kwargs)
        fake = FakeSession(routes)
        client._session = fake  # type: ignore[assignment]
        return client, fake

    return build
