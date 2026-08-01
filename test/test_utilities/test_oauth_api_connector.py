"""Tests for OAuth2APIConnector.

These pin its public behavior (token fetch at init, bearer header on requests,
APIConnector-compatible helper methods) and verify that it shares the base
connector's session, so it inherits timeouts, retries, and rate limiting.
"""

import pytest
import requests
import urllib3
from requests.adapters import HTTPAdapter

import parsons.utilities.api_connector as api_connector_module
from test.test_utilities.conftest import BASE_URI, TOKEN_URL

# Token Fetch


def test_fetches_token_at_init(oauth_connector, requests_mock):
    connector = oauth_connector()
    assert connector.token["access_token"] == "fake-token"
    token_request = requests_mock.request_history[0]
    assert token_request.url == TOKEN_URL
    assert "grant_type=client_credentials" in token_request.text


def test_custom_grant_type(oauth_connector, requests_mock):
    oauth_connector(grant_type="account_credentials")
    assert "grant_type=account_credentials" in requests_mock.request_history[0].text


def test_authorization_kwargs_forwarded_to_token_fetch(oauth_connector, requests_mock):
    oauth_connector(authorization_kwargs={"audience": "https://api.example.com"})
    assert "audience=" in requests_mock.request_history[0].text


def test_authorization_kwargs_timeout_does_not_collide(oauth_connector, requests_mock):
    # Historically the only way to set a token-fetch timeout; must not
    # raise a duplicate-keyword TypeError now that timeout= is explicit.
    connector = oauth_connector(authorization_kwargs={"timeout": 30})
    assert connector.token["access_token"] == "fake-token"


def test_token_saver_updates_token(oauth_connector, requests_mock):
    connector = oauth_connector()
    connector.token_saver({"access_token": "rotated"})
    assert connector.token == {"access_token": "rotated"}


# Request


def test_bearer_token_sent(oauth_connector, requests_mock):
    connector = oauth_connector()
    requests_mock.get(BASE_URI + "things", json={"a": 1})
    resp = connector.request("things", "GET")
    assert resp.request.headers["Authorization"] == "Bearer fake-token"


def test_relative_url_joined(oauth_connector, requests_mock):
    connector = oauth_connector()
    requests_mock.get(BASE_URI + "things", json={})
    resp = connector.request("things", "GET")
    assert resp.request.url == BASE_URI + "things"


def test_returns_raw_response(oauth_connector, requests_mock):
    connector = oauth_connector()
    requests_mock.get(BASE_URI + "things", json={"a": 1})
    resp = connector.request("things", "GET")
    assert isinstance(resp, requests.Response)
    assert resp.json() == {"a": 1}


def test_headers_forwarded(oauth_connector, requests_mock):
    connector = oauth_connector(headers={"X-Custom": "yes"})
    requests_mock.get(BASE_URI + "things", json={})
    resp = connector.request("things", "GET")
    assert resp.request.headers["X-Custom"] == "yes"


# The APIConnector helper surface works unchanged through OAuth2.


def test_get_request_returns_json(oauth_connector, requests_mock):
    connector = oauth_connector()
    requests_mock.get(BASE_URI + "things", json={"a": 1})
    assert connector.get_request("things") == {"a": 1}


def test_post_request_dual_return(oauth_connector, requests_mock):
    connector = oauth_connector()
    requests_mock.post(BASE_URI + "things", json={"id": 5}, status_code=201)
    requests_mock.post(BASE_URI + "empty", status_code=204)
    assert connector.post_request("things", json={}) == {"id": 5}
    assert connector.post_request("empty") == 204


def test_get_request_raises_on_error(oauth_connector, requests_mock):
    connector = oauth_connector()
    requests_mock.get(BASE_URI + "things", status_code=500, reason="Server Error")
    with pytest.raises(requests.exceptions.HTTPError):
        connector.get_request("things")


def test_data_key_parsing(oauth_connector, requests_mock):
    connector = oauth_connector(data_key="results")
    assert connector.data_parse({"results": [{"a": 1}]}) == [{"a": 1}]


# OAuth2APIConnector runs through the base session, so the reliability options configured on APIConnector apply to it too.


def test_uses_its_oauth_session_as_the_base_session(oauth_connector, requests_mock):
    connector = oauth_connector()
    assert connector.session is connector.client


def test_timeout_is_applied_to_requests(oauth_connector, requests_mock):
    connector = oauth_connector(timeout=(10, 120))
    requests_mock.get(BASE_URI + "things", json={})
    connector.get("things")
    assert requests_mock.last_request.timeout == (10, 120)


def test_retries_are_configured_on_the_oauth_session(oauth_connector, requests_mock):
    connector = oauth_connector(retries=3)
    retry = connector.client.get_adapter(BASE_URI).max_retries
    assert retry.total == 3
    assert retry.allowed_methods == ("GET", "HEAD", "OPTIONS")


def test_accepts_a_retry_object_like_the_base_class(oauth_connector, requests_mock):
    custom = urllib3.util.Retry(total=7)
    connector = oauth_connector(retries=custom)
    assert connector.client.get_adapter(BASE_URI).max_retries is custom


def test_rate_limit_interval_throttles_requests(oauth_connector, requests_mock, monkeypatch):
    sleeps = []
    monkeypatch.setattr(api_connector_module, "_sleep", sleeps.append)
    connector = oauth_connector(rate_limit_interval=10)
    requests_mock.get(BASE_URI + "things", json={})

    connector.get("things")
    assert sleeps == []  # the first request is never throttled

    connector.get("things")
    assert len(sleeps) == 1
    assert 0 < sleeps[0] <= 10


def test_token_fetch_honors_the_connector_timeout(oauth_connector, requests_mock):
    connector = oauth_connector(timeout=30)
    token_request = requests_mock.request_history[0]
    assert token_request.url == TOKEN_URL
    assert token_request.timeout == 30
    assert connector.token["access_token"] == "fake-token"


def test_custom_adapters_on_the_oauth_session_are_preserved(oauth_connector, requests_mock):
    # retries= configures the session's existing adapters rather than
    # replacing them, so an adapter mounted by a connector survives.
    connector = oauth_connector()
    custom = HTTPAdapter(pool_maxsize=99)
    connector.client.mount("https://", custom)
    assert connector.client.get_adapter(BASE_URI) is custom
