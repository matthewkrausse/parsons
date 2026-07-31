"""Characterization tests for APIConnector.

These tests lock in the existing public behavior of APIConnector so that
internal refactors (sessions, timeouts, retries) can be verified not to
change it. If a change to APIConnector forces an assertion change here,
that change is a behavior change and must be declared in the PR.
"""

import pytest
import requests
from requests.exceptions import HTTPError

from parsons import Table
from parsons.utilities.api_connector import APIConnector

BASE_URI = "https://api.example.com/v1"
BASE_URI_SLASH = "https://api.example.com/v1/"


@pytest.fixture
def connector():
    return APIConnector(BASE_URI)


class TestInit:
    def test_trailing_slash_added(self):
        assert APIConnector(BASE_URI).uri == BASE_URI_SLASH

    def test_trailing_slash_preserved(self):
        assert APIConnector(BASE_URI_SLASH).uri == BASE_URI_SLASH

    def test_defaults(self, connector):
        assert connector.headers is None
        assert connector.auth is None
        assert connector.pagination_key is None
        assert connector.data_key is None

    def test_stores_arguments(self):
        headers = {"X-Api-Key": "abc"}
        auth = ("user", "pass")
        conn = APIConnector(
            BASE_URI, headers=headers, auth=auth, pagination_key="next", data_key="results"
        )
        assert conn.headers == headers
        assert conn.auth == auth
        assert conn.pagination_key == "next"
        assert conn.data_key == "results"


class TestRequest:
    def test_returns_raw_response(self, requests_mock, connector):
        m = requests_mock
        m.get(BASE_URI_SLASH + "things", json={"a": 1})
        resp = connector.request("things", "GET")
        assert isinstance(resp, requests.Response)
        assert resp.json() == {"a": 1}

    def test_relative_url_joined_to_uri(self, requests_mock, connector):
        m = requests_mock
        m.get(BASE_URI_SLASH + "things", json={})
        resp = connector.request("things", "GET")
        assert resp.request.url == BASE_URI_SLASH + "things"

    def test_absolute_url_used_as_is(self, requests_mock, connector):
        m = requests_mock
        m.get("https://other.example.com/elsewhere", json={})
        resp = connector.request("https://other.example.com/elsewhere", "GET")
        assert resp.request.url == "https://other.example.com/elsewhere"

    def test_urljoin_replaces_last_path_segment_of_base(self, requests_mock):
        m = requests_mock
        # urljoin("https://api.example.com/v1/", "other/path") keeps the base path;
        # a leading slash resets to the host root. Characterize both.
        conn = APIConnector(BASE_URI_SLASH)
        m.get("https://api.example.com/v1/other/path", json={})
        m.get("https://api.example.com/rooted", json={})
        assert (
            conn.request("other/path", "GET").request.url == "https://api.example.com/v1/other/path"
        )
        assert conn.request("/rooted", "GET").request.url == "https://api.example.com/rooted"

    def test_headers_and_params_and_json_forwarded(self, requests_mock):
        m = requests_mock
        conn = APIConnector(BASE_URI, headers={"X-Api-Key": "abc"})
        m.post(BASE_URI_SLASH + "things", json={})
        resp = conn.request("things", "POST", json={"k": "v"}, params={"id": "1"})
        sent = resp.request
        assert sent.headers["X-Api-Key"] == "abc"
        assert "id=1" in sent.url
        assert sent.body == b'{"k": "v"}'

    def test_basic_auth_forwarded(self, requests_mock):
        m = requests_mock
        conn = APIConnector(BASE_URI, auth=("user", "pass"))
        m.get(BASE_URI_SLASH + "things", json={})
        resp = conn.request("things", "GET")
        assert resp.request.headers["Authorization"].startswith("Basic ")

    def test_error_status_does_not_raise(self, requests_mock, connector):
        m = requests_mock
        # request(raise_on_error=False) returns the error response instead of raising.
        m.get(BASE_URI_SLASH + "things", status_code=500)
        resp = connector.request("things", "GET", raise_on_error=False)
        assert resp.status_code == 500


class TestGetRequest:
    def test_returns_parsed_json_dict(self, requests_mock, connector):
        m = requests_mock
        m.get(BASE_URI_SLASH + "things", json={"a": 1})
        assert connector.get_request("things") == {"a": 1}

    def test_returns_parsed_json_list(self, requests_mock, connector):
        m = requests_mock
        m.get(BASE_URI_SLASH + "things", json=[{"a": 1}, {"a": 2}])
        assert connector.get_request("things") == [{"a": 1}, {"a": 2}]

    def test_return_format_content_returns_bytes(self, requests_mock, connector):
        m = requests_mock
        # community.py depends on return_format="content"
        m.get(BASE_URI_SLASH + "export", content=b"col_a,col_b\n1,2\n")
        assert connector.get_request("export", return_format="content") == b"col_a,col_b\n1,2\n"

    def test_invalid_return_format_raises_runtime_error(self, requests_mock, connector):
        m = requests_mock
        m.get(BASE_URI_SLASH + "things", json={})
        with pytest.raises(RuntimeError, match="not a valid format"):
            connector.get_request("things", return_format="pickle")

    def test_raises_http_error_on_4xx(self, requests_mock, connector):
        m = requests_mock
        m.get(BASE_URI_SLASH + "things", status_code=404, reason="Not Found")
        with pytest.raises(HTTPError):
            connector.get_request("things")

    def test_params_forwarded(self, requests_mock, connector):
        m = requests_mock
        m.get(BASE_URI_SLASH + "things", json={})
        connector.get_request("things", params={"page": "2"})
        assert "page=2" in m.last_request.url


class TestPostRequest:
    def test_returns_json_when_body_has_json(self, requests_mock, connector):
        m = requests_mock
        m.post(BASE_URI_SLASH + "things", json={"id": 99}, status_code=201)
        assert connector.post_request("things", json={"name": "x"}) == {"id": 99}

    def test_returns_status_code_when_no_json_body(self, requests_mock, connector):
        m = requests_mock
        m.post(BASE_URI_SLASH + "things", status_code=204)
        assert connector.post_request("things") == 204

    def test_default_success_codes_include_202(self, requests_mock, connector):
        m = requests_mock
        m.post(BASE_URI_SLASH + "things", status_code=202)
        assert connector.post_request("things") == 202

    def test_returns_none_when_status_not_in_success_codes(self, requests_mock, connector):
        m = requests_mock
        # 200 response but caller only accepts 201: falls through, returns None.
        m.post(BASE_URI_SLASH + "things", json={"id": 99}, status_code=200)
        assert connector.post_request("things", success_codes=[201]) is None

    def test_custom_success_codes(self, requests_mock, connector):
        m = requests_mock
        m.post(BASE_URI_SLASH + "things", json={"ok": True}, status_code=207)
        assert connector.post_request("things", success_codes=[207]) == {"ok": True}

    def test_raises_http_error_on_error_status(self, requests_mock, connector):
        m = requests_mock
        m.post(BASE_URI_SLASH + "things", status_code=400, reason="Bad Request")
        with pytest.raises(HTTPError):
            connector.post_request("things")

    def test_data_forwarded(self, requests_mock, connector):
        m = requests_mock
        m.post(BASE_URI_SLASH + "things", status_code=204)
        connector.post_request("things", data="raw-payload")
        assert m.last_request.text == "raw-payload"


class TestPutRequest:
    def test_returns_json_when_body_has_json(self, requests_mock, connector):
        m = requests_mock
        m.put(BASE_URI_SLASH + "things/1", json={"id": 1})
        assert connector.put_request("things/1", json={"name": "x"}) == {"id": 1}

    def test_returns_status_code_when_no_json_body(self, requests_mock, connector):
        m = requests_mock
        m.put(BASE_URI_SLASH + "things/1", status_code=204)
        assert connector.put_request("things/1") == 204

    def test_202_in_default_success_codes(self, requests_mock, connector):
        m = requests_mock
        # Success codes are unified across verbs to [200, 201, 202, 204].
        m.put(BASE_URI_SLASH + "things/1", status_code=202)
        assert connector.put_request("things/1") == 202

    def test_raises_http_error_on_error_status(self, requests_mock, connector):
        m = requests_mock
        m.put(BASE_URI_SLASH + "things/1", status_code=500, reason="Server Error")
        with pytest.raises(HTTPError):
            connector.put_request("things/1")


class TestPatchRequest:
    def test_returns_json_when_body_has_json(self, requests_mock, connector):
        m = requests_mock
        m.patch(BASE_URI_SLASH + "things/1", json={"id": 1})
        assert connector.patch_request("things/1", json={"name": "x"}) == {"id": 1}

    def test_returns_status_code_when_no_json_body(self, requests_mock, connector):
        m = requests_mock
        m.patch(BASE_URI_SLASH + "things/1", status_code=204)
        assert connector.patch_request("things/1") == 204


class TestDeleteRequest:
    def test_returns_json_when_body_has_json(self, requests_mock, connector):
        m = requests_mock
        m.delete(BASE_URI_SLASH + "things/1", json={"deleted": True})
        assert connector.delete_request("things/1") == {"deleted": True}

    def test_returns_status_code_when_no_json_body(self, requests_mock, connector):
        m = requests_mock
        m.delete(BASE_URI_SLASH + "things/1", status_code=204)
        assert connector.delete_request("things/1") == 204

    def test_raises_http_error_on_error_status(self, requests_mock, connector):
        m = requests_mock
        m.delete(BASE_URI_SLASH + "things/1", status_code=403, reason="Forbidden")
        with pytest.raises(HTTPError):
            connector.delete_request("things/1")


class TestValidateResponse:
    """Pin the exact error message formats other code may be matching on."""

    def test_no_raise_below_400(self, requests_mock, connector):
        m = requests_mock
        m.get(BASE_URI_SLASH + "things", status_code=399)
        connector.validate_response(connector.request("things", "GET"))

    def test_message_with_reason(self, requests_mock, connector):
        m = requests_mock
        m.get(BASE_URI_SLASH + "things", status_code=500, reason="Server Error")
        resp = connector.request("things", "GET", raise_on_error=False)
        with pytest.raises(HTTPError, match=r"Code: 500; URL: .*; Reason: Server Error"):
            connector.validate_response(resp)

    def test_message_with_text_when_no_reason(self, requests_mock, connector):
        m = requests_mock
        m.get(BASE_URI_SLASH + "things", status_code=500, reason=None, text="boom")
        resp = connector.request("things", "GET", raise_on_error=False)
        with pytest.raises(HTTPError, match=r"Code: 500; URL: .*; Text: boom"):
            connector.validate_response(resp)

    def test_message_bare_when_no_reason_or_text(self, requests_mock, connector):
        m = requests_mock
        m.get(BASE_URI_SLASH + "things", status_code=500, reason=None)
        resp = connector.request("things", "GET", raise_on_error=False)
        with pytest.raises(HTTPError, match=r"Code: 500; URL:"):
            connector.validate_response(resp)

    def test_message_appends_json_body(self, requests_mock, connector):
        m = requests_mock
        m.get(
            BASE_URI_SLASH + "things",
            status_code=429,
            reason="Too Many Requests",
            json={"error": "rate limited"},
        )
        resp = connector.request("things", "GET", raise_on_error=False)
        with pytest.raises(HTTPError, match=r"JSON: \{'error': 'rate limited'\}"):
            connector.validate_response(resp)


class TestJsonCheck:
    def test_true_when_json_body(self, requests_mock, connector):
        m = requests_mock
        m.get(BASE_URI_SLASH + "things", json={"a": 1})
        assert connector.json_check(connector.request("things", "GET")) is True

    def test_false_when_not_json(self, requests_mock, connector):
        m = requests_mock
        m.get(BASE_URI_SLASH + "things", text="<html></html>")
        assert connector.json_check(connector.request("things", "GET")) is False

    def test_false_when_empty_body(self, requests_mock, connector):
        m = requests_mock
        m.get(BASE_URI_SLASH + "things", status_code=204)
        assert connector.json_check(connector.request("things", "GET")) is False


class TestDataParse:
    def test_list_returned_as_is(self, connector):
        data = [{"a": 1}]
        assert APIConnector(BASE_URI, data_key="results").data_parse(data) is data

    def test_data_key_extracts_nested_data(self):
        conn = APIConnector(BASE_URI, data_key="results")
        assert conn.data_parse({"results": [{"a": 1}], "meta": {}}) == [{"a": 1}]

    def test_missing_data_key_returns_whole_dict(self):
        conn = APIConnector(BASE_URI, data_key="results")
        resp = {"other": 1}
        assert conn.data_parse(resp) is resp

    def test_no_data_key_returns_whole_dict(self, connector):
        resp = {"a": 1}
        assert connector.data_parse(resp) is resp


class TestNextPageCheckUrl:
    def test_true_when_pagination_key_truthy(self):
        conn = APIConnector(BASE_URI, pagination_key="next")
        assert conn.next_page_check_url({"next": "https://api.example.com/v1/things?page=2"})

    def test_falsy_when_pagination_key_empty(self):
        # NOTE: current implementation returns None (not False) here.
        conn = APIConnector(BASE_URI, pagination_key="next")
        assert not conn.next_page_check_url({"next": ""})

    def test_false_when_pagination_key_missing(self):
        conn = APIConnector(BASE_URI, pagination_key="next")
        assert conn.next_page_check_url({"other": 1}) is False

    def test_false_when_no_pagination_key_configured(self, connector):
        assert connector.next_page_check_url({"next": "url"}) is False


class TestConvertToTable:
    def test_list_of_dicts(self, connector):
        tbl = connector.convert_to_table([{"a": 1}, {"a": 2}])
        assert isinstance(tbl, Table)
        assert tbl.num_rows == 2

    def test_single_dict_wrapped(self, connector):
        tbl = connector.convert_to_table({"a": 1})
        assert isinstance(tbl, Table)
        assert tbl.num_rows == 1
