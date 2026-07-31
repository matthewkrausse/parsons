"""Characterization tests for OAuth2APIConnector.

These lock in the existing public behavior (token fetch at init, bearer
header on requests, APIConnector-compatible helper methods) so internal
refactors can be verified not to change it.
"""

import pytest
import requests

from parsons.utilities.oauth_api_connector import OAuth2APIConnector

BASE_URI = "https://api.example.com/v1/"
TOKEN_URL = "https://auth.example.com/oauth/token"

CLIENT_ID = "fake-client-id"
CLIENT_SECRET = "fake-client-secret"


def make_connector(m, token_response=None, **kwargs):
    m.post(TOKEN_URL, json=token_response or {"access_token": "fake-token", "token_type": "Bearer"})
    return OAuth2APIConnector(
        BASE_URI,
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        token_url=TOKEN_URL,
        auto_refresh_url=None,
        **kwargs,
    )


class TestTokenFetch:
    def test_fetches_token_at_init(self, requests_mock):
        m = requests_mock
        connector = make_connector(m)
        assert connector.token["access_token"] == "fake-token"
        token_request = m.request_history[0]
        assert token_request.url == TOKEN_URL
        assert "grant_type=client_credentials" in token_request.text

    def test_custom_grant_type(self, requests_mock):
        m = requests_mock
        make_connector(m, grant_type="account_credentials")
        assert "grant_type=account_credentials" in m.request_history[0].text

    def test_authorization_kwargs_forwarded_to_token_fetch(self, requests_mock):
        m = requests_mock
        make_connector(m, authorization_kwargs={"audience": "https://api.example.com"})
        assert "audience=" in m.request_history[0].text

    def test_authorization_kwargs_timeout_does_not_collide(self, requests_mock):
        # Historically the only way to set a token-fetch timeout; must not
        # raise a duplicate-keyword TypeError now that timeout= is explicit.
        m = requests_mock
        connector = make_connector(m, authorization_kwargs={"timeout": 30})
        assert connector.token["access_token"] == "fake-token"

    def test_token_saver_updates_token(self, requests_mock):
        m = requests_mock
        connector = make_connector(m)
        connector.token_saver({"access_token": "rotated"})
        assert connector.token == {"access_token": "rotated"}


class TestRequest:
    def test_bearer_token_sent(self, requests_mock):
        m = requests_mock
        connector = make_connector(m)
        m.get(BASE_URI + "things", json={"a": 1})
        resp = connector.request("things", "GET")
        assert resp.request.headers["Authorization"] == "Bearer fake-token"

    def test_relative_url_joined(self, requests_mock):
        m = requests_mock
        connector = make_connector(m)
        m.get(BASE_URI + "things", json={})
        resp = connector.request("things", "GET")
        assert resp.request.url == BASE_URI + "things"

    def test_returns_raw_response(self, requests_mock):
        m = requests_mock
        connector = make_connector(m)
        m.get(BASE_URI + "things", json={"a": 1})
        resp = connector.request("things", "GET")
        assert isinstance(resp, requests.Response)
        assert resp.json() == {"a": 1}

    def test_headers_forwarded(self, requests_mock):
        m = requests_mock
        connector = make_connector(m, headers={"X-Custom": "yes"})
        m.get(BASE_URI + "things", json={})
        resp = connector.request("things", "GET")
        assert resp.request.headers["X-Custom"] == "yes"


class TestInheritedHelpers:
    """The APIConnector helper surface works unchanged through OAuth2."""

    def test_get_request_returns_json(self, requests_mock):
        m = requests_mock
        connector = make_connector(m)
        m.get(BASE_URI + "things", json={"a": 1})
        assert connector.get_request("things") == {"a": 1}

    def test_post_request_dual_return(self, requests_mock):
        m = requests_mock
        connector = make_connector(m)
        m.post(BASE_URI + "things", json={"id": 5}, status_code=201)
        m.post(BASE_URI + "empty", status_code=204)
        assert connector.post_request("things", json={}) == {"id": 5}
        assert connector.post_request("empty") == 204

    def test_get_request_raises_on_error(self, requests_mock):
        m = requests_mock
        connector = make_connector(m)
        m.get(BASE_URI + "things", status_code=500, reason="Server Error")
        with pytest.raises(requests.exceptions.HTTPError):
            connector.get_request("things")

    def test_data_key_parsing(self, requests_mock):
        m = requests_mock
        connector = make_connector(m, data_key="results")
        assert connector.data_parse({"results": [{"a": 1}]}) == [{"a": 1}]
