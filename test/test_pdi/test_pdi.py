import contextlib
import os

import pytest

from parsons import PDI


#
# Fixtures and constants
#
def remove_from_env(*env_vars):
    for var in env_vars:
        with contextlib.suppress(KeyError):
            del os.environ[var]


#
# Tests
#


# Need to provide environment variables
# PDI_USERNAME, PDI_PASSWORD, PDI_API_TOKEN
@pytest.mark.live
def test_connection():
    PDI(qa_url=True)


@pytest.mark.parametrize(
    ("username", "password", "api_token"),
    [
        (None, None, None),
        (None, "pass", "token"),
        ("user", None, "token"),
        ("user", "pass", None),
    ],
)
def test_init_error(username, password, api_token):
    remove_from_env("PDI_USERNAME", "PDI_PASSWORD", "PDI_API_TOKEN")
    with pytest.raises(KeyError):
        PDI(username, password, api_token)


@pytest.mark.parametrize(
    ("obj", "exp_obj"),
    [
        ({"a": "a", "b": None, "c": "c"}, {"a": "a", "c": "c"}),
        (
            [{"a": "a", "b": None, "c": "c"}, {"a": "a", "c": None}],
            [{"a": "a", "c": "c"}, {"a": "a"}],
        ),
        ("string", "string"),
    ],
)
def test_clean_dict(mock_pdi, obj, exp_obj):
    assert mock_pdi._clean_dict(obj) == exp_obj


def test_session_token_fetched_lazily_and_sent(mock_pdi, requests_mock):
    # Lazy auth: no token request happens at construction.
    assert not any(r.url.endswith("/sessions") for r in requests_mock.request_history)

    requests_mock.get("https://apiqa.bluevote.com/thing", json={"foo": "bar"})
    result = mock_pdi._request("https://apiqa.bluevote.com/thing")

    assert result == {"foo": "bar"}
    session_calls = [r for r in requests_mock.request_history if r.url.endswith("/sessions")]
    assert len(session_calls) == 1  # fetched once, on first use
    assert requests_mock.last_request.headers["Authorization"] == "Bearer AccessToken"


def test_request_paginates_with_cursor(mock_pdi, requests_mock):
    # Unbounded read: follow the 1-indexed cursor until len(data) == totalCount.
    requests_mock.get(
        "https://apiqa.bluevote.com/things",
        [
            {"json": {"data": [{"id": 1}, {"id": 2}], "totalCount": 3}},
            {"json": {"data": [{"id": 3}], "totalCount": 3}},
        ],
    )

    result = mock_pdi._request("https://apiqa.bluevote.com/things")

    assert result.num_rows == 3
    assert [row["id"] for row in result] == [1, 2, 3]
    # The second page request advances the cursor to 2.
    assert requests_mock.last_request.qs["cursor"] == ["2"]
