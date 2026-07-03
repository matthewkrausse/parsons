"""Tests for the pluggable auth helpers in parsons.utilities.auth."""

import math
import time

import pytest
import requests

from parsons.utilities.api_connector import APIConnector
from parsons.utilities.auth import ExpiringTokenAuth, HeaderTokenAuth

BASE_URI = "https://api.example.com/v1/"


def apply(auth):
    """Run an auth object against a prepared request and return its headers."""
    req = requests.Request("GET", BASE_URI).prepare()
    return auth(req).headers


class TestHeaderTokenAuth:
    def test_default_bearer_header(self):
        headers = apply(HeaderTokenAuth("tok"))
        assert headers["Authorization"] == "Bearer tok"

    def test_custom_header_and_template(self):
        headers = apply(HeaderTokenAuth("tok", header="X-Api-Key", template="{token}"))
        assert headers["X-Api-Key"] == "tok"

    def test_works_through_api_connector(self, requests_mock):
        conn = APIConnector(BASE_URI, auth=HeaderTokenAuth("tok"))
        requests_mock.get(BASE_URI + "things", json={})
        conn.get("things")
        assert requests_mock.last_request.headers["Authorization"] == "Bearer tok"


class TestExpiringTokenAuth:
    def test_fetches_lazily_and_caches(self):
        calls = []

        def fetch():
            calls.append(1)
            return f"tok{len(calls)}", 3600

        auth = ExpiringTokenAuth(fetch)
        assert not calls  # nothing fetched at construction
        assert apply(auth)["Authorization"] == "Bearer tok1"
        assert apply(auth)["Authorization"] == "Bearer tok1"
        assert len(calls) == 1

    def test_refreshes_before_expiry(self, monkeypatch):
        calls = []

        def fetch():
            calls.append(1)
            return f"tok{len(calls)}", 100

        auth = ExpiringTokenAuth(fetch, refresh_margin=60)
        now = time.monotonic()
        apply(auth)
        assert len(calls) == 1

        # 30s in: 70s of ttl left, more than the 60s margin -- no refresh.
        monkeypatch.setattr(time, "monotonic", lambda: now + 30)
        apply(auth)
        assert len(calls) == 1

        # 50s in: 50s of ttl left, inside the margin -- refresh.
        monkeypatch.setattr(time, "monotonic", lambda: now + 50)
        assert apply(auth)["Authorization"] == "Bearer tok2"
        assert len(calls) == 2

    def test_none_ttl_never_refreshes(self, monkeypatch):
        calls = []

        def fetch():
            calls.append(1)
            return "tok", None

        auth = ExpiringTokenAuth(fetch)
        apply(auth)
        monkeypatch.setattr(time, "monotonic", lambda: math.inf - 1)
        apply(auth)
        assert len(calls) == 1

    def test_subclass_override(self):
        class MyAuth(ExpiringTokenAuth):
            def fetch_token(self):
                return "subclassed", 3600

        assert apply(MyAuth())["Authorization"] == "Bearer subclassed"

    def test_requires_fetch_token(self):
        with pytest.raises(NotImplementedError):
            apply(ExpiringTokenAuth())

    def test_works_through_api_connector(self, requests_mock):
        auth = ExpiringTokenAuth(lambda: ("tok", 3600), template="Token {token}")
        conn = APIConnector(BASE_URI, auth=auth)
        requests_mock.get(BASE_URI + "things", json={})
        conn.get("things")
        assert requests_mock.last_request.headers["Authorization"] == "Token tok"
