# Parsons HTTP layer (`APIConnector`)

`APIConnector` is the shared HTTP client that Parsons REST connectors are built
on. It wraps [`requests`](https://requests.readthedocs.io/) with a single,
consistent surface for authentication, pagination, timeouts, retries, and
error handling, so individual connectors don't each hand-roll those concerns.

If you are writing or maintaining a connector that talks to a JSON REST API,
build it on `APIConnector` (or `OAuth2APIConnector`) rather than calling
`requests` directly.

- Code: [`api_connector.py`](api_connector.py), [`oauth_api_connector.py`](oauth_api_connector.py), [`auth.py`](auth.py), [`pagination.py`](pagination.py), [`api_exceptions.py`](api_exceptions.py)
- New connectors should build on this layer. Migrating existing connectors onto
  it is a follow-up effort (one connector per PR); this reference is the
  starting point.

---

## Quick start

Connectors use `APIConnector` by **composition** — hold one as `self.client`
and call through it:

```python
from parsons.utilities.api_connector import APIConnector


class MyService:
    def __init__(self, api_key):
        self.client = APIConnector(
            "https://api.myservice.com/v1/",   # base uri, trailing slash added if missing
            headers={"Accept": "application/json"},
            auth=("user", api_key),            # see "Authentication" below
        )

    def get_widget(self, widget_id):
        # Relative URLs are joined onto the base uri.
        return self.client.get(f"widgets/{widget_id}").json()
```

`get()` returns a [`requests.Response`](https://requests.readthedocs.io/en/latest/api/#requests.Response)
and raises on an error status code (see [Errors](#errors)).

---

## Authentication

Pass whatever the API needs as `auth=` (any [`requests` auth](https://requests.readthedocs.io/en/latest/user/authentication/)
object or a `(user, password)` tuple) and/or `headers=`. Parsons ships auth
helpers in [`auth.py`](auth.py) for the common token patterns.

| The API expects… | Use | Example |
|---|---|---|
| HTTP Basic (user/password, or key-as-user) | a tuple | `auth=(api_key, "x")` |
| A static token in a header | `HeaderTokenAuth` | `auth=HeaderTokenAuth(token)` |
| A token fetched from a login endpoint that expires | `ExpiringTokenAuth` | see below |
| OAuth2 client-credentials | `OAuth2APIConnector` | see below |
| An API key in the query string | no auth object | pass it in each call's `params=`, or bake it into the base uri |

### `HeaderTokenAuth`

A static token placed in a header. Defaults to `Authorization: Bearer <token>`;
override `header` and `template` for APIs that differ.

```python
from parsons.utilities.auth import HeaderTokenAuth

# Authorization: Bearer abc123
APIConnector(uri, auth=HeaderTokenAuth("abc123"))

# x-api-key: abc123   (custom header, bare token, no scheme)
APIConnector(uri, auth=HeaderTokenAuth("abc123", header="x-api-key", template="{token}"))

# Authorization: Key abc123
APIConnector(uri, auth=HeaderTokenAuth("abc123", template="Key {token}"))
```

### `ExpiringTokenAuth`

For APIs where you exchange credentials at a login endpoint for a token that
expires. The token is fetched lazily on the first request and re-fetched
`refresh_margin` seconds *before* it expires — so a request is never sent with
a stale token and no failed request has to be replayed. Provide a `fetch_token`
callable (or subclass and override `fetch_token`) returning
`(token, ttl_seconds)`; use `ttl_seconds=None` for tokens that never expire.

```python
from parsons.utilities.auth import ExpiringTokenAuth

def fetch():
    resp = requests.post(LOGIN_URL, json={"user": u, "password": p})
    body = resp.json()
    return body["access_token"], body["expires_in"]   # (token, ttl seconds)

APIConnector(uri, auth=ExpiringTokenAuth(fetch))
```

### `OAuth2APIConnector`

For OAuth2 client-credentials (server-to-server) APIs. It fetches a token at
construction, sends it on every request, and transparently re-fetches on
expiry. It otherwise provides the same request/pagination surface as
`APIConnector`, and inherits its `timeout`, `retries`, and
`rate_limit_interval` options.

```python
from parsons.utilities.oauth_api_connector import OAuth2APIConnector

self.client = OAuth2APIConnector(
    uri="https://api.example.com/v1/",
    client_id=client_id,
    client_secret=client_secret,
    token_url="https://api.example.com/oauth/token",
    auto_refresh_url="https://api.example.com/oauth/token",
    grant_type="client_credentials",              # or a vendor grant
    authorization_kwargs={"audience": "..."},     # extra token-fetch params
)
```

Non-client-credentials flows (e.g. authorization-code) are **not** covered by
`OAuth2APIConnector` — keep those connector-specific.

---

## Making requests

### Verb methods (preferred)

`get` / `post` / `put` / `patch` / `delete` each return a `requests.Response`
and raise a [`ParsonsHTTPError`](#errors) on a `>= 400` status. Arguments are
keyword-only after the url.

```python
resp = self.client.get("widgets", params={"active": "true"})
widgets = resp.json()["results"]

self.client.post("widgets", json={"name": "new"})          # send a JSON body
self.client.delete("widgets/5")                            # .status_code == 204
```

`Response` is the return type because it is the only thing that serves every
caller — JSON bodies (`.json()`), binary (`.content`), headers, `.links`, and
`.status_code`.

Per-request overrides and passthroughs:

```python
self.client.get("slow-report", timeout=(10, 300))     # override the client timeout
self.client.get("thing", headers={"X-Trace": "1"})    # merged over client headers
self.client.post("upload", files={"f": fh})           # **kwargs go straight to requests
self.client.get("export", stream=True)
```

### The `*_request` methods

`get_request`, `post_request`, `put_request`, `patch_request`, and
`delete_request` predate the verb methods and keep their existing signatures
and return types, so the many connectors that use them are unaffected. They
return *parsed* results, not a `Response`:

- `get_request(url, params=None, return_format="json")` → parsed JSON (`dict`/`list`), or raw `bytes` when `return_format="content"`.
- `post_request` / `put_request` / `patch_request` / `delete_request` → the parsed JSON body if the response has one, otherwise the integer status code.

Each also takes `raise_on_error` (default `True`). Validation now happens in one
place — inside `request()` — so passing `raise_on_error=False` to `get_request`
returns the parsed error body instead of raising; previously `get_request`
raised regardless. No in-tree caller passes that flag to `get_request`.

New code should prefer the verb methods (one consistent return type). Existing
`*_request` calls are fine to leave in place.

---

## Pagination

`paginate(url, paginator, *, params=None, max_pages=None, **kwargs)` issues the
first request and follows the API's pagination until the last page, yielding
each page's `Response`:

```python
from parsons.utilities.pagination import LinkHeaderPaginator

rows = []
for response in self.client.paginate("tickets", LinkHeaderPaginator(), params={"per_page": 100}):
    rows.extend(response.json())
```

Pick the paginator that matches how the API signals "next page":

| The API signals the next page by… | Use | Key argument |
|---|---|---|
| a `Link:` response header (RFC 5988) | `LinkHeaderPaginator(rel="next")` | link relation |
| a next-page URL in the response body | `NextUrlPaginator(next_key)` | dotted path, e.g. `"pagination.next_url"`, `"_links.next.href"` |
| an opaque cursor echoed back as a query param | `CursorPaginator(cursor_key, cursor_param)` | body path + query-param name |
| an incrementing page number | `PageNumberPaginator(page_param, ...)` | see below |

`PageNumberPaginator` supports several stop conditions:

```python
# stop when the response's "more" boolean is falsy (most precise; when
# more_key is set, data_key and page_size are not consulted)
PageNumberPaginator(page_param="page", more_key="more")

# otherwise stop on an empty page, or a short page when page_size is known
PageNumberPaginator(page_param="page", page_size=100)
```

`data_key`, `next_key`, `cursor_key`, and `more_key` all accept **dotted paths**
into nested JSON (e.g. `"result.pagination.nextPage"`). Relative next-page URLs
are resolved against the page that returned them.

Paginators are stateless — one instance is safe to reuse. `max_pages=` is a
safety valve against runaway pagination.

---

## Reliability: timeouts, retries, rate limiting

These are constructor options on `APIConnector` (and `OAuth2APIConnector`):

```python
from parsons.utilities.api_connector import DEFAULT_TIMEOUT, default_retry

APIConnector(
    uri,
    timeout=DEFAULT_TIMEOUT,        # (10, 120) = (connect, read) seconds; or a single number
    retries=default_retry(),        # or an int, or a urllib3.util.Retry
    rate_limit_interval=1.0,        # min seconds between requests
)
```

- **`timeout`** — a `(connect, read)` tuple or single number. The read timeout
  is *between bytes*, not total duration, so large downloads are safe; only
  endpoints that take longer than the read timeout to send their **first** byte
  need a per-request override (or `timeout=None`).
- **`retries`** — pass an int for the standard policy (`default_retry(total)`)
  or a `urllib3.util.Retry` for full control. The standard policy retries
  transient failures (`429, 500, 502, 503, 504`) with exponential backoff,
  honors the server's `Retry-After` header on `429`, and **only retries
  `GET`, `HEAD`, and `OPTIONS`** — a deliberately conservative default. `POST`
  is never auto-retried because a duplicate `POST` (a donation, a signup) can
  be worse than a failed one; `PUT` and `DELETE` are idempotent in principle but
  are left out by default because not every API implements them that way. Widen
  `allowed_methods` per connector where you have verified it is safe. Retries
  are invisible to callers except that transient failures now succeed; the same
  `HTTPError` is raised if they're exhausted.
- **`rate_limit_interval`** — enforce a minimum gap between the requests this
  connector issues (replaces hand-rolled `time.sleep()` calls). It spaces the
  calls made through `request()`; it does not throttle retries performed inside
  the adapter, which are paced by the retry policy's own backoff.

---

## Errors

On a `>= 400` status, the verb methods and `*_request` methods raise a
`ParsonsHTTPError`, which **subclasses `requests.exceptions.HTTPError`** — so
existing `except requests.exceptions.HTTPError` handlers keep working. The
offending response is attached as `.response`. Two subclasses let you branch on
the common cases (see [`api_exceptions.py`](api_exceptions.py)):

```python
from parsons.utilities.api_exceptions import RateLimitError, AuthenticationError

try:
    self.client.get("thing")
except RateLimitError as e:       # HTTP 429
    wait = e.retry_after          # seconds from the Retry-After header, or None
except AuthenticationError:       # HTTP 401
    ...
```

---

## Testing a connector

The canonical guide for writing connector tests is
[`docs/contrib_docs/write_tests.rst`](../../docs/contrib_docs/write_tests.rst) — follow it for test
structure (plain pytest functions, per-connector `conftest.py`, canned payloads
under `data/`) and the "mock the outermost boundary you don't own" rule. For a
connector built on `APIConnector`, that boundary is the HTTP layer, so tests use
the [`requests_mock`](https://requests-mock.readthedocs.io/) fixture; internal
`Session`s are transparent to it:

```python
from parsons import MyService


def test_get_widget(requests_mock):
    requests_mock.get("https://api.myservice.com/v1/widgets/5", json={"id": 5})
    assert MyService("KEY").get_widget(5)["id"] == 5


def test_pagination(requests_mock):
    # a response list drives multiple pages
    requests_mock.get("https://api.myservice.com/v1/widgets", [
        {"json": {"results": [1, 2]}, "headers": {"Link": '<...&page=2>; rel="next"'}},
        {"json": {"results": [3]}},
    ])
    ...
```

Retries live *below* the transport `requests_mock` replaces, so retry behavior
is verified separately (adapter-config assertions plus a stdlib `http.server`
integration test) — see `test/test_utilities/test_api_connector*.py`.

---

## Escape hatches (what the client does not do)

- **Async job / polling APIs** (submit a job, poll until ready, download a
  file): keep the poll loop in the connector — there's no shared paginator for
  it — but issue the requests through the client so they get timeouts/retries.
- **Streaming / multipart uploads**: pass `stream=`, `files=`, etc. as
  `**kwargs` on any verb method; they go straight to `requests`.
- **Non-JSON bodies** (CSV, XML, ZIP): use `get(...).content` (or the legacy
  `get_request(..., return_format="content")`) and parse in the connector.
- **Vendor SDKs**: connectors built on a vendor SDK (boto3, PyGithub, …) let the
  SDK own transport and do not use `APIConnector`.
