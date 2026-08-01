"""Tests for the established APIConnector surface.

These pin the public behavior of the connector's request helpers — return
types, success codes, URL joining, and error-message formats — so that changes
to the internals (sessions, timeouts, retries) can be shown not to alter it.
"""

import pytest
import requests
from requests.exceptions import HTTPError

from parsons import Table
from parsons.utilities.api_connector import APIConnector
from test.test_utilities.conftest import BASE_URI, BASE_URI_NO_SLASH

# Init


def test_trailing_slash_added():
    assert APIConnector(BASE_URI_NO_SLASH).uri == BASE_URI


def test_trailing_slash_preserved():
    assert APIConnector(BASE_URI).uri == BASE_URI


def test_defaults(connector):
    assert connector.headers is None
    assert connector.auth is None
    assert connector.pagination_key is None
    assert connector.data_key is None


def test_stores_arguments():
    headers = {"X-Api-Key": "abc"}
    auth = ("user", "pass")
    conn = APIConnector(
        BASE_URI_NO_SLASH, headers=headers, auth=auth, pagination_key="next", data_key="results"
    )
    assert conn.headers == headers
    assert conn.auth == auth
    assert conn.pagination_key == "next"
    assert conn.data_key == "results"


# Request


def test_returns_raw_response(requests_mock, connector):
    requests_mock.get(BASE_URI + "things", json={"a": 1})
    resp = connector.request("things", "GET")
    assert isinstance(resp, requests.Response)
    assert resp.json() == {"a": 1}


def test_relative_url_joined_to_uri(requests_mock, connector):
    requests_mock.get(BASE_URI + "things", json={})
    resp = connector.request("things", "GET")
    assert resp.request.url == BASE_URI + "things"


def test_absolute_url_used_as_is(requests_mock, connector):
    requests_mock.get("https://other.example.com/elsewhere", json={})
    resp = connector.request("https://other.example.com/elsewhere", "GET")
    assert resp.request.url == "https://other.example.com/elsewhere"


def test_urljoin_replaces_last_path_segment_of_base(requests_mock):
    # urljoin("https://api.example.com/v1/", "other/path") keeps the base path;
    # a leading slash resets to the host root. Characterize both.
    conn = APIConnector(BASE_URI)
    requests_mock.get("https://api.example.com/v1/other/path", json={})
    requests_mock.get("https://api.example.com/rooted", json={})
    assert conn.request("other/path", "GET").request.url == "https://api.example.com/v1/other/path"
    assert conn.request("/rooted", "GET").request.url == "https://api.example.com/rooted"


def test_headers_and_params_and_json_forwarded(requests_mock):
    conn = APIConnector(BASE_URI_NO_SLASH, headers={"X-Api-Key": "abc"})
    requests_mock.post(BASE_URI + "things", json={})
    resp = conn.request("things", "POST", json={"k": "v"}, params={"id": "1"})
    sent = resp.request
    assert sent.headers["X-Api-Key"] == "abc"
    assert "id=1" in sent.url
    assert sent.body == b'{"k": "v"}'


def test_basic_auth_forwarded(requests_mock):
    conn = APIConnector(BASE_URI_NO_SLASH, auth=("user", "pass"))
    requests_mock.get(BASE_URI + "things", json={})
    resp = conn.request("things", "GET")
    assert resp.request.headers["Authorization"].startswith("Basic ")


def test_error_status_does_not_raise(requests_mock, connector):
    # request(raise_on_error=False) returns the error response instead of raising.
    requests_mock.get(BASE_URI + "things", status_code=500)
    resp = connector.request("things", "GET", raise_on_error=False)
    assert resp.status_code == 500


# Get Request


def test_get_request_returns_parsed_json_dict(requests_mock, connector):
    requests_mock.get(BASE_URI + "things", json={"a": 1})
    assert connector.get_request("things") == {"a": 1}


def test_get_request_returns_parsed_json_list(requests_mock, connector):
    requests_mock.get(BASE_URI + "things", json=[{"a": 1}, {"a": 2}])
    assert connector.get_request("things") == [{"a": 1}, {"a": 2}]


def test_get_request_return_format_content_returns_bytes(requests_mock, connector):
    # community.py depends on return_format="content"
    requests_mock.get(BASE_URI + "export", content=b"col_a,col_b\n1,2\n")
    assert connector.get_request("export", return_format="content") == b"col_a,col_b\n1,2\n"


def test_get_request_invalid_return_format_raises_runtime_error(requests_mock, connector):
    requests_mock.get(BASE_URI + "things", json={})
    with pytest.raises(RuntimeError, match="not a valid format"):
        connector.get_request("things", return_format="pickle")


def test_get_request_raises_http_error_on_4xx(requests_mock, connector):
    requests_mock.get(BASE_URI + "things", status_code=404, reason="Not Found")
    with pytest.raises(HTTPError):
        connector.get_request("things")


def test_get_request_params_forwarded(requests_mock, connector):
    requests_mock.get(BASE_URI + "things", json={})
    connector.get_request("things", params={"page": "2"})
    assert "page=2" in requests_mock.last_request.url


# Post Request


def test_post_request_returns_json_when_body_has_json(requests_mock, connector):
    requests_mock.post(BASE_URI + "things", json={"id": 99}, status_code=201)
    assert connector.post_request("things", json={"name": "x"}) == {"id": 99}


def test_post_request_returns_status_code_when_no_json_body(requests_mock, connector):
    requests_mock.post(BASE_URI + "things", status_code=204)
    assert connector.post_request("things") == 204


def test_post_request_default_success_codes_include_202(requests_mock, connector):
    requests_mock.post(BASE_URI + "things", status_code=202)
    assert connector.post_request("things") == 202


def test_post_request_returns_none_when_status_not_in_success_codes(requests_mock, connector):
    # 200 response but caller only accepts 201: falls through, returns None.
    requests_mock.post(BASE_URI + "things", json={"id": 99}, status_code=200)
    assert connector.post_request("things", success_codes=[201]) is None


def test_post_request_custom_success_codes(requests_mock, connector):
    requests_mock.post(BASE_URI + "things", json={"ok": True}, status_code=207)
    assert connector.post_request("things", success_codes=[207]) == {"ok": True}


def test_post_request_raises_http_error_on_error_status(requests_mock, connector):
    requests_mock.post(BASE_URI + "things", status_code=400, reason="Bad Request")
    with pytest.raises(HTTPError):
        connector.post_request("things")


def test_post_request_data_forwarded(requests_mock, connector):
    requests_mock.post(BASE_URI + "things", status_code=204)
    connector.post_request("things", data="raw-payload")
    assert requests_mock.last_request.text == "raw-payload"


# Put Request


def test_put_request_returns_json_when_body_has_json(requests_mock, connector):
    requests_mock.put(BASE_URI + "things/1", json={"id": 1})
    assert connector.put_request("things/1", json={"name": "x"}) == {"id": 1}


def test_put_request_returns_status_code_when_no_json_body(requests_mock, connector):
    requests_mock.put(BASE_URI + "things/1", status_code=204)
    assert connector.put_request("things/1") == 204


def test_put_request_202_in_default_success_codes(requests_mock, connector):
    # Success codes are unified across verbs to [200, 201, 202, 204].
    requests_mock.put(BASE_URI + "things/1", status_code=202)
    assert connector.put_request("things/1") == 202


def test_put_request_raises_http_error_on_error_status(requests_mock, connector):
    requests_mock.put(BASE_URI + "things/1", status_code=500, reason="Server Error")
    with pytest.raises(HTTPError):
        connector.put_request("things/1")


# Patch Request


def test_patch_request_returns_json_when_body_has_json(requests_mock, connector):
    requests_mock.patch(BASE_URI + "things/1", json={"id": 1})
    assert connector.patch_request("things/1", json={"name": "x"}) == {"id": 1}


def test_patch_request_returns_status_code_when_no_json_body(requests_mock, connector):
    requests_mock.patch(BASE_URI + "things/1", status_code=204)
    assert connector.patch_request("things/1") == 204


# Delete Request


def test_delete_request_returns_json_when_body_has_json(requests_mock, connector):
    requests_mock.delete(BASE_URI + "things/1", json={"deleted": True})
    assert connector.delete_request("things/1") == {"deleted": True}


def test_delete_request_returns_status_code_when_no_json_body(requests_mock, connector):
    requests_mock.delete(BASE_URI + "things/1", status_code=204)
    assert connector.delete_request("things/1") == 204


def test_delete_request_raises_http_error_on_error_status(requests_mock, connector):
    requests_mock.delete(BASE_URI + "things/1", status_code=403, reason="Forbidden")
    with pytest.raises(HTTPError):
        connector.delete_request("things/1")


# Pin the exact error message formats other code may be matching on.


def test_no_raise_below_400(requests_mock, connector):
    requests_mock.get(BASE_URI + "things", status_code=399)
    connector.validate_response(connector.request("things", "GET"))


def test_message_with_reason(requests_mock, connector):
    requests_mock.get(BASE_URI + "things", status_code=500, reason="Server Error")
    resp = connector.request("things", "GET", raise_on_error=False)
    with pytest.raises(HTTPError, match=r"Code: 500; URL: .*; Reason: Server Error"):
        connector.validate_response(resp)


def test_message_with_text_when_no_reason(requests_mock, connector):
    requests_mock.get(BASE_URI + "things", status_code=500, reason=None, text="boom")
    resp = connector.request("things", "GET", raise_on_error=False)
    with pytest.raises(HTTPError, match=r"Code: 500; URL: .*; Text: boom"):
        connector.validate_response(resp)


def test_message_bare_when_no_reason_or_text(requests_mock, connector):
    requests_mock.get(BASE_URI + "things", status_code=500, reason=None)
    resp = connector.request("things", "GET", raise_on_error=False)
    with pytest.raises(HTTPError) as excinfo:
        connector.validate_response(resp)
    # With neither reason nor body there is nothing to append: the message
    # is only the code and url.
    assert str(excinfo.value) == f"Code: 500; URL: {BASE_URI}things"


def test_message_appends_json_body(requests_mock, connector):
    requests_mock.get(
        BASE_URI + "things",
        status_code=429,
        reason="Too Many Requests",
        json={"error": "rate limited"},
    )
    resp = connector.request("things", "GET", raise_on_error=False)
    with pytest.raises(HTTPError, match=r"JSON: \{'error': 'rate limited'\}"):
        connector.validate_response(resp)


# Json Check


def test_true_when_json_body(requests_mock, connector):
    requests_mock.get(BASE_URI + "things", json={"a": 1})
    assert connector.json_check(connector.request("things", "GET")) is True


def test_false_when_not_json(requests_mock, connector):
    requests_mock.get(BASE_URI + "things", text="<html></html>")
    assert connector.json_check(connector.request("things", "GET")) is False


def test_false_when_empty_body(requests_mock, connector):
    requests_mock.get(BASE_URI + "things", status_code=204)
    assert connector.json_check(connector.request("things", "GET")) is False


# Data Parse


def test_list_returned_as_is(connector):
    data = [{"a": 1}]
    assert APIConnector(BASE_URI_NO_SLASH, data_key="results").data_parse(data) is data


def test_data_key_extracts_nested_data():
    conn = APIConnector(BASE_URI_NO_SLASH, data_key="results")
    assert conn.data_parse({"results": [{"a": 1}], "meta": {}}) == [{"a": 1}]


def test_missing_data_key_returns_whole_dict():
    conn = APIConnector(BASE_URI_NO_SLASH, data_key="results")
    resp = {"other": 1}
    assert conn.data_parse(resp) is resp


def test_no_data_key_returns_whole_dict(connector):
    resp = {"a": 1}
    assert connector.data_parse(resp) is resp


# Next Page Check Url


def test_true_when_pagination_key_truthy():
    conn = APIConnector(BASE_URI_NO_SLASH, pagination_key="next")
    assert conn.next_page_check_url({"next": "https://api.example.com/v1/things?page=2"})


def test_falsy_when_pagination_key_empty():
    # NOTE: current implementation returns None (not False) here.
    conn = APIConnector(BASE_URI_NO_SLASH, pagination_key="next")
    assert not conn.next_page_check_url({"next": ""})


def test_false_when_pagination_key_missing():
    conn = APIConnector(BASE_URI_NO_SLASH, pagination_key="next")
    assert conn.next_page_check_url({"other": 1}) is False


def test_false_when_no_pagination_key_configured(connector):
    assert connector.next_page_check_url({"next": "url"}) is False


# Convert To Table


def test_list_of_dicts(connector):
    tbl = connector.convert_to_table([{"a": 1}, {"a": 2}])
    assert isinstance(tbl, Table)
    assert tbl.num_rows == 2


def test_single_dict_wrapped(connector):
    tbl = connector.convert_to_table({"a": 1})
    assert isinstance(tbl, Table)
    assert tbl.num_rows == 1
