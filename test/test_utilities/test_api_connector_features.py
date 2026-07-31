"""Tests for the modern APIConnector surface.

Covers the Response-returning verb methods, typed exceptions, retry/timeout
configuration, request throttling, session injection, pagination, and the
OAuth2 token-expiry retry. (The legacy surface is covered by the
characterization tests in test_api_connector.py.)
"""

import pytest
import requests
import urllib3
from requests.adapters import HTTPAdapter

import parsons.utilities.api_connector as api_connector_module
from parsons.utilities.api_connector import APIConnector, default_retry
from parsons.utilities.api_exceptions import (
    AuthenticationError,
    ParsonsHTTPError,
    RateLimitError,
)
from parsons.utilities.oauth_api_connector import OAuth2APIConnector
from parsons.utilities.pagination import (
    CursorPaginator,
    LinkHeaderPaginator,
    NextUrlPaginator,
    PageNumberPaginator,
)

BASE_URI = "https://api.example.com/v1/"


@pytest.fixture
def connector():
    return APIConnector(BASE_URI)


class TestVerbMethods:
    def test_get_returns_response(self, requests_mock, connector):
        requests_mock.get(BASE_URI + "things", json={"a": 1})
        resp = connector.get("things")
        assert isinstance(resp, requests.Response)
        assert resp.json() == {"a": 1}
        assert resp.status_code == 200

    def test_post_sends_json_and_returns_response(self, requests_mock, connector):
        requests_mock.post(BASE_URI + "things", json={"id": 7}, status_code=201)
        resp = connector.post("things", json={"name": "x"})
        assert resp.status_code == 201
        assert requests_mock.last_request.json() == {"name": "x"}

    def test_put_patch_delete(self, requests_mock, connector):
        requests_mock.put(BASE_URI + "things/1", status_code=204)
        requests_mock.patch(BASE_URI + "things/1", json={"id": 1})
        requests_mock.delete(BASE_URI + "things/1", status_code=204)
        assert connector.put("things/1", json={}).status_code == 204
        assert connector.patch("things/1", json={}).json() == {"id": 1}
        assert connector.delete("things/1").status_code == 204

    def test_delete_accepts_body(self, requests_mock, connector):
        # The legacy delete_request has no body support; the verb method does.
        requests_mock.delete(BASE_URI + "things", status_code=204)
        connector.delete("things", json={"ids": [1, 2]})
        assert requests_mock.last_request.json() == {"ids": [1, 2]}

    def test_verbs_raise_typed_errors(self, requests_mock, connector):
        requests_mock.get(BASE_URI + "things", status_code=500, reason="Server Error")
        with pytest.raises(ParsonsHTTPError):
            connector.get("things")

    def test_per_request_headers_merge_over_connector_headers(self, requests_mock):
        conn = APIConnector(BASE_URI, headers={"X-One": "a", "X-Two": "b"})
        requests_mock.get(BASE_URI + "things", json={})
        conn.get("things", headers={"X-Two": "override"})
        sent = requests_mock.last_request
        assert sent.headers["X-One"] == "a"
        assert sent.headers["X-Two"] == "override"

    def test_kwargs_passed_through(self, requests_mock, connector):
        requests_mock.get(BASE_URI + "things", json={})
        connector.get("things", stream=True)
        assert requests_mock.last_request.stream is True


class TestTimeout:
    def test_default_is_no_timeout(self, requests_mock, connector):
        requests_mock.get(BASE_URI + "things", json={})
        connector.get("things")
        assert requests_mock.last_request.timeout is None

    def test_connector_timeout_used(self, requests_mock):
        conn = APIConnector(BASE_URI, timeout=(10, 120))
        requests_mock.get(BASE_URI + "things", json={})
        conn.get("things")
        assert requests_mock.last_request.timeout == (10, 120)

    def test_per_request_override(self, requests_mock):
        conn = APIConnector(BASE_URI, timeout=(10, 120))
        requests_mock.get(BASE_URI + "things", json={})
        conn.get("things", timeout=5)
        assert requests_mock.last_request.timeout == 5

    def test_per_request_none_disables(self, requests_mock):
        conn = APIConnector(BASE_URI, timeout=(10, 120))
        requests_mock.get(BASE_URI + "things", json={})
        conn.get("things", timeout=None)
        assert requests_mock.last_request.timeout is None


class TestTypedExceptions:
    def test_429_raises_rate_limit_error_with_retry_after(self, requests_mock, connector):
        requests_mock.get(
            BASE_URI + "things",
            status_code=429,
            reason="Too Many Requests",
            headers={"Retry-After": "30"},
        )
        with pytest.raises(RateLimitError) as excinfo:
            connector.get("things")
        assert excinfo.value.retry_after == 30.0
        assert excinfo.value.response.status_code == 429

    def test_retry_after_http_date_form(self, requests_mock, connector):
        requests_mock.get(
            BASE_URI + "things",
            status_code=429,
            reason="Too Many Requests",
            headers={"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"},
        )
        with pytest.raises(RateLimitError) as excinfo:
            connector.get("things")
        # A date in the past clamps to 0 rather than going negative.
        assert excinfo.value.retry_after == 0.0

    def test_retry_after_zoneless_http_date(self, requests_mock, connector):
        # A date without a zone parses to a naive datetime; it must not raise.
        requests_mock.get(
            BASE_URI + "things",
            status_code=429,
            reason="Too Many Requests",
            headers={"Retry-After": "Wed, 21 Oct 2015 07:28:00"},
        )
        with pytest.raises(RateLimitError) as excinfo:
            connector.get("things")
        assert excinfo.value.retry_after == 0.0

    def test_retry_after_absent(self, requests_mock, connector):
        requests_mock.get(BASE_URI + "things", status_code=429, reason="Too Many Requests")
        with pytest.raises(RateLimitError) as excinfo:
            connector.get("things")
        assert excinfo.value.retry_after is None

    def test_401_raises_authentication_error(self, requests_mock, connector):
        requests_mock.get(BASE_URI + "things", status_code=401, reason="Unauthorized")
        with pytest.raises(AuthenticationError):
            connector.get("things")

    def test_typed_errors_are_http_errors(self, requests_mock, connector):
        # Existing `except HTTPError` handlers must keep working.
        requests_mock.get(BASE_URI + "things", status_code=404, reason="Not Found")
        with pytest.raises(requests.exceptions.HTTPError) as excinfo:
            connector.get("things")
        assert isinstance(excinfo.value, ParsonsHTTPError)
        assert excinfo.value.response is not None

    def test_legacy_methods_raise_typed_errors_too(self, requests_mock, connector):
        requests_mock.get(BASE_URI + "things", status_code=429, reason="Too Many Requests")
        with pytest.raises(RateLimitError):
            connector.get_request("things")


class TestRetryConfiguration:
    """Retries happen below the mocked transport, so assert the mounted config."""

    def test_no_retries_by_default(self):
        conn = APIConnector(BASE_URI)
        adapter = conn.session.get_adapter(BASE_URI)
        assert adapter.max_retries.total == 0

    def test_int_builds_default_policy(self):
        conn = APIConnector(BASE_URI, retries=5)
        for scheme in ("https://", "http://"):
            retry = conn.session.get_adapter(scheme).max_retries
            assert retry.total == 5
            assert retry.status_forcelist == (429, 500, 502, 503, 504)
            assert retry.allowed_methods == ("GET", "HEAD", "OPTIONS")
            assert retry.raise_on_status is False
            assert retry.respect_retry_after_header is True

    def test_custom_retry_object_honored(self):
        custom = urllib3.util.Retry(total=7, backoff_factor=2)
        conn = APIConnector(BASE_URI, retries=custom)
        assert conn.session.get_adapter(BASE_URI).max_retries is custom

    def test_default_retry_never_retries_post(self):
        assert "POST" not in default_retry().allowed_methods


class TestThrottle:
    def test_sleeps_between_requests(self, requests_mock, monkeypatch):
        sleeps = []
        monkeypatch.setattr(api_connector_module, "_sleep", sleeps.append)
        conn = APIConnector(BASE_URI, rate_limit_interval=10)
        requests_mock.get(BASE_URI + "things", json={})

        conn.get("things")
        assert sleeps == []  # first request is never throttled

        conn.get("things")
        assert len(sleeps) == 1
        assert 9 < sleeps[0] <= 10

    def test_no_throttle_by_default(self, requests_mock, monkeypatch):
        sleeps = []
        monkeypatch.setattr(api_connector_module, "_sleep", sleeps.append)
        conn = APIConnector(BASE_URI)
        requests_mock.get(BASE_URI + "things", json={})
        conn.get("things")
        conn.get("things")
        assert sleeps == []


class TestSessionInjection:
    def test_injected_session_is_used(self, requests_mock):
        session = requests.Session()
        conn = APIConnector(BASE_URI, session=session)
        assert conn.session is session

    def test_retries_mount_on_injected_session(self):
        session = requests.Session()
        APIConnector(BASE_URI, retries=3, session=session)
        assert session.get_adapter(BASE_URI).max_retries.total == 3

    def test_retries_preserve_custom_adapter_on_injected_session(self):
        # A caller-injected session's custom adapter must survive retries=,
        # with the retry policy applied to it rather than being replaced.
        session = requests.Session()
        custom = HTTPAdapter(pool_maxsize=99)
        session.mount("https://", custom)
        APIConnector(BASE_URI, retries=3, session=session)
        adapter = session.get_adapter(BASE_URI)
        assert adapter is custom
        assert adapter.max_retries.total == 3


class TestPaginate:
    def test_link_header_pagination(self, requests_mock, connector):
        page2 = BASE_URI + "things?page=2"
        requests_mock.get(
            BASE_URI + "things",
            [
                {
                    "json": {"items": [1, 2]},
                    "headers": {"Link": f'<{page2}>; rel="next"'},
                },
                {"json": {"items": [3]}},
            ],
        )
        pages = list(connector.paginate("things", LinkHeaderPaginator()))
        assert [p.json()["items"] for p in pages] == [[1, 2], [3]]
        assert requests_mock.request_history[1].url == page2

    def test_next_url_pagination_with_dotted_key(self, requests_mock, connector):
        page2 = BASE_URI + "things?cursor=abc"
        requests_mock.get(
            BASE_URI + "things",
            [
                {"json": {"data": [1], "_links": {"next": {"href": page2}}}},
                {"json": {"data": [2], "_links": {}}},
            ],
        )
        pages = list(connector.paginate("things", NextUrlPaginator("_links.next.href")))
        assert len(pages) == 2
        assert requests_mock.request_history[1].url == page2

    def test_link_header_relative_url_resolves_against_page(self, requests_mock, connector):
        # A relative next URL must resolve against the page that returned it,
        # keeping the 'things' path, not against the connector base URI.
        requests_mock.get(
            BASE_URI + "things",
            [
                {"json": {"items": [1]}, "headers": {"Link": '<?page=2>; rel="next"'}},
                {"json": {"items": [2]}},
            ],
        )
        pages = list(connector.paginate("things", LinkHeaderPaginator()))
        assert len(pages) == 2
        assert requests_mock.request_history[1].url == BASE_URI + "things?page=2"

    def test_next_url_relative_resolves_against_page(self, requests_mock, connector):
        requests_mock.get(
            BASE_URI + "things",
            [
                {"json": {"data": [1], "next": "?page=2"}},
                {"json": {"data": [2]}},
            ],
        )
        pages = list(connector.paginate("things", NextUrlPaginator()))
        assert len(pages) == 2
        assert requests_mock.request_history[1].url == BASE_URI + "things?page=2"

    def test_cursor_pagination_preserves_params(self, requests_mock, connector):
        requests_mock.get(
            BASE_URI + "things",
            [
                {"json": {"data": [1], "cursors": {"after": "abc"}}},
                {"json": {"data": [2], "cursors": {}}},
            ],
        )
        pages = list(
            connector.paginate(
                "things",
                CursorPaginator("cursors.after", "after"),
                params={"per_page": "50"},
            )
        )
        assert len(pages) == 2
        second = requests_mock.request_history[1]
        assert second.qs["after"] == ["abc"]
        assert second.qs["per_page"] == ["50"]

    def test_cursor_pagination_stops_on_string_more_flag(self, requests_mock, connector):
        # Some APIs (e.g. Hustle) keep returning a cursor and signal the end
        # with a stringy boolean; more_key must treat "false" as falsy.
        requests_mock.get(
            BASE_URI + "things",
            [
                {"json": {"data": [1], "pagination": {"cursor": "c1", "hasNextPage": "true"}}},
                {"json": {"data": [2], "pagination": {"cursor": "c2", "hasNextPage": "false"}}},
            ],
        )
        pages = list(
            connector.paginate(
                "things",
                CursorPaginator("pagination.cursor", "cursor", more_key="pagination.hasNextPage"),
            )
        )
        # Stops after the page whose hasNextPage is the string "false", even
        # though that page still carries a cursor.
        assert len(pages) == 2
        assert requests_mock.request_history[1].qs["cursor"] == ["c1"]

    def test_page_number_pagination_stops_on_empty_page(self, requests_mock, connector):
        requests_mock.get(
            BASE_URI + "things",
            [
                {"json": {"results": [1, 2]}},
                {"json": {"results": [3]}},
                {"json": {"results": []}},
            ],
        )
        pages = list(
            connector.paginate(
                "things",
                PageNumberPaginator(data_key="results"),
                params={"page": 1},
            )
        )
        assert len(pages) == 3
        assert [r.qs["page"] for r in requests_mock.request_history] == [["1"], ["2"], ["3"]]

    def test_page_number_stops_on_more_flag(self, requests_mock, connector):
        requests_mock.get(
            BASE_URI + "things",
            [
                {"json": {"results": {"a": 1}, "more": True}},
                {"json": {"results": {"b": 2}, "more": False}},
            ],
        )
        pages = list(
            connector.paginate(
                "things",
                PageNumberPaginator(more_key="more"),
                params={"page": 1},
            )
        )
        assert len(pages) == 2
        assert [r.qs["page"] for r in requests_mock.request_history] == [["1"], ["2"]]

    def test_page_number_short_page_saves_a_request(self, requests_mock, connector):
        requests_mock.get(
            BASE_URI + "things",
            [
                {"json": [1, 2, 3]},
                {"json": [4]},
            ],
        )
        pages = list(
            connector.paginate(
                "things",
                PageNumberPaginator(page_size=3),
                params={"page": 1},
            )
        )
        assert len(pages) == 2

    def test_max_pages_safety_valve(self, requests_mock, connector):
        requests_mock.get(
            BASE_URI + "things",
            json={"data": [1], "cursors": {"after": "always-more"}},
        )
        pages = list(
            connector.paginate("things", CursorPaginator("cursors.after", "after"), max_pages=5)
        )
        assert len(pages) == 5

    def test_pagination_errors_propagate(self, requests_mock, connector):
        requests_mock.get(BASE_URI + "things", status_code=500, reason="Server Error")
        with pytest.raises(ParsonsHTTPError):
            list(connector.paginate("things", LinkHeaderPaginator()))


class TestOAuth2TokenExpiryRetry:
    TOKEN_URL = "https://auth.example.com/oauth/token"

    def test_expired_token_is_refetched_and_request_retried(self, requests_mock):
        requests_mock.post(
            self.TOKEN_URL,
            [
                # Already expired when the first API call happens.
                {"json": {"access_token": "stale", "token_type": "Bearer", "expires_in": -100}},
                {"json": {"access_token": "fresh", "token_type": "Bearer", "expires_in": 3600}},
            ],
        )
        connector = OAuth2APIConnector(
            BASE_URI,
            client_id="id",
            client_secret="secret",
            token_url=self.TOKEN_URL,
            auto_refresh_url=None,
        )
        requests_mock.get(BASE_URI + "things", json={"ok": True})

        assert connector.get_request("things") == {"ok": True}
        assert connector.token["access_token"] == "fresh"
        assert requests_mock.last_request.headers["Authorization"] == "Bearer fresh"
        token_fetches = [r for r in requests_mock.request_history if r.url == self.TOKEN_URL]
        assert len(token_fetches) == 2

    def test_post_is_not_duplicated_on_token_refresh(self, requests_mock):
        # TokenExpiredError is raised client-side before the request is sent,
        # so the refresh-and-retry is safe for POST: the body reaches the
        # server exactly once.
        requests_mock.post(
            self.TOKEN_URL,
            [
                {"json": {"access_token": "stale", "token_type": "Bearer", "expires_in": -100}},
                {"json": {"access_token": "fresh", "token_type": "Bearer", "expires_in": 3600}},
            ],
        )
        connector = OAuth2APIConnector(
            BASE_URI,
            client_id="id",
            client_secret="secret",
            token_url=self.TOKEN_URL,
            auto_refresh_url=None,
        )
        requests_mock.post(BASE_URI + "things", json={"ok": True}, status_code=201)

        assert connector.post_request("things", json={"amount": 100}) == {"ok": True}
        api_posts = [r for r in requests_mock.request_history if r.url == BASE_URI + "things"]
        assert len(api_posts) == 1
        assert api_posts[0].headers["Authorization"] == "Bearer fresh"


class TestIsTruthy:
    """The stringy-boolean interpreter behind the paginators' more_key."""

    @pytest.mark.parametrize("value", [True, 1, "true", "True", "yes", "1", "anything"])
    def test_truthy(self, value):
        from parsons.utilities.pagination import _is_truthy

        assert _is_truthy(value) is True

    @pytest.mark.parametrize("value", [False, 0, None, "", "false", "False", "0", "no", "  "])
    def test_falsy(self, value):
        from parsons.utilities.pagination import _is_truthy

        assert _is_truthy(value) is False
