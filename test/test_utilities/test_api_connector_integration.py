"""End-to-end tests for APIConnector retry and timeout behavior.

Retries live in the urllib3 layer below the transport adapter that
requests_mock replaces, so these tests exercise them against a real (local,
threaded, stdlib-only) HTTP server instead.
"""

import http.server
import threading
import time

import pytest
import requests
import urllib3

from parsons.utilities.api_connector import APIConnector, default_retry
from parsons.utilities.api_exceptions import ParsonsHTTPError


class ScriptedHandler(http.server.BaseHTTPRequestHandler):
    """Serves a scripted list of (status, headers, delay) responses in order.

    Once the script is exhausted, keeps serving the last entry. Records every
    request as (method, path).
    """

    script = []
    requests_seen = []

    def _respond(self):
        index = min(len(self.requests_seen), len(self.script) - 1)
        self.requests_seen.append((self.command, self.path))
        status, headers, delay = self.script[index]
        if delay:
            time.sleep(delay)
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        body = b'{"ok": true}'
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = _respond
    do_POST = _respond

    def log_message(self, *args):
        pass


@pytest.fixture
def local_server():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), ScriptedHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def scripted(script):
        ScriptedHandler.script = script
        ScriptedHandler.requests_seen = []
        return f"http://127.0.0.1:{server.server_address[1]}/"

    yield scripted
    server.shutdown()
    thread.join()


def make_connector(uri, **kwargs):
    connector = APIConnector(uri, **kwargs)
    # Keep any ambient proxy configuration out of the test.
    connector.session.trust_env = False
    return connector


# Retry Behavior


def test_transient_503s_are_retried_until_success(local_server):
    uri = local_server(
        [
            (503, {}, 0),
            (503, {}, 0),
            (200, {}, 0),
        ]
    )
    retry = urllib3.util.Retry(
        total=3, backoff_factor=0, status_forcelist=[503], raise_on_status=False
    )
    connector = make_connector(uri, retries=retry)

    response = connector.get("things")

    assert response.json() == {"ok": True}
    assert len(ScriptedHandler.requests_seen) == 3


def test_exhausted_retries_return_final_response_and_raise_http_error(local_server):
    uri = local_server([(503, {}, 0)])
    retry = urllib3.util.Retry(
        total=2, backoff_factor=0, status_forcelist=[503], raise_on_status=False
    )
    connector = make_connector(uri, retries=retry)

    with pytest.raises(ParsonsHTTPError, match=r"Code: 503"):
        connector.get("things")
    assert len(ScriptedHandler.requests_seen) == 3  # original + 2 retries


def test_post_is_not_retried_by_default_policy(local_server):
    uri = local_server([(503, {}, 0)])
    retry = default_retry(3)
    retry.backoff_factor = 0
    connector = make_connector(uri, retries=retry)

    with pytest.raises(ParsonsHTTPError):
        connector.post("things", json={"amount": 100})
    assert len(ScriptedHandler.requests_seen) == 1


def test_retry_after_header_is_honored(local_server):
    uri = local_server(
        [
            (429, {"Retry-After": "1"}, 0),
            (200, {}, 0),
        ]
    )
    retry = default_retry(2)
    retry.backoff_factor = 0
    connector = make_connector(uri, retries=retry)

    started = time.monotonic()
    response = connector.get("things")
    elapsed = time.monotonic() - started

    assert response.json() == {"ok": True}
    assert len(ScriptedHandler.requests_seen) == 2
    assert elapsed >= 0.9


# Timeout Behavior


def test_read_timeout_raises(local_server):
    uri = local_server([(200, {}, 1.5)])
    connector = make_connector(uri, timeout=(5, 0.5))

    with pytest.raises(requests.exceptions.ReadTimeout):
        connector.get("things")


def test_per_request_timeout_overrides_connector_default(local_server):
    uri = local_server([(200, {}, 1.5)])
    connector = make_connector(uri, timeout=(5, 0.5))

    response = connector.get("things", timeout=(5, 5))
    assert response.json() == {"ok": True}
