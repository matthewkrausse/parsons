# Migrating connectors onto the standard `APIConnector`

This is a working guide for moving Parsons' REST connectors onto the shared
HTTP layer ([`APIConnector`](../parsons/utilities/README.md)) so they all make
consistent calls — with pooled connections, timeouts, retries, `Retry-After`
handling, and shared pagination — instead of each hand-rolling those concerns.

It doubles as the **status tracker** for the effort. Once every connector has
migrated, this file can be removed.

> New to the HTTP layer itself? Read the [layer reference](../parsons/utilities/README.md)
> first; this guide assumes it.

## Why

The shared client used to be a thin wrapper over `requests.request()`: no
`Session`, no timeout, no retries, and inconsistent return types. Connectors
compensated by hand-rolling pagination (four different ways), token refresh,
and rate-limit `sleep()`s. The refactor hardens the client and gives connectors
one way to do each of these; migrating a connector means **deleting** its
bespoke plumbing in favor of the shared primitives.

## Status tracker

Legend: ✅ merged · 🔵 POC (this batch) · ⬜ not started · ⛔ out of scope (vendor SDK / non-HTTP).

| Connector | Auth family | Pagination | Status | PR |
|---|---|---|---|---|
| freshdesk | basic-auth | link-header | ✅ | #3 |
| quickbooks | header-token | page-number | ✅ | #3 |
| targetsmart | header-token | none | ✅ | #3 |
| crowdtangle | api-key-param | next-url-in-body | ✅ | #3 |
| actblue | basic-auth | polling | ✅ | #3 |
| hustle | oauth2 | cursor | ✅ | #3 |
| pdi | expiring-token | page-number | ✅ | #3 |
| capitol_canary | basic-auth | next-url-in-body | ⚠️ | see note |
| action_builder | header-token | page-number | ⬜ | — |
| action_network | header-token | page-number | ⬜ | — |
| action_kit | basic-auth | next-url-in-body | ⬜ | — |
| airmeet | expiring-token | cursor | ⬜ | — |
| auth0 | oauth2 | polling | ⬜ | — |
| bill_com | expiring-token | page-number | ⬜ | — |
| bloomerang | header-token / oauth2 | page-number | ⬜ | — |
| catalist | oauth2 | polling | ⬜ | — |
| census | api-key-param | none | ⬜ | — |
| community | header-token | none | ⬜ | — |
| controlshift | oauth2 | page-number | ⬜ | — |
| copper | header-token | page-number | ⬜ | — |
| donorbox | basic-auth | page-number | ⬜ | — |
| empower | header-token | none | ⬜ | — |
| formstack | header-token | page-number | ⬜ | — |
| mailchimp | basic-auth | page-number | ⬜ | — |
| mobilecommons | header-token | page-number (XML) | ⬜ | — |
| mobilize_america | header-token (optional) | next-url-in-body | ⬜ | — |
| nation_builder | header-token | next-url-in-body | ⬜ | — |
| newmode | oauth2 | next-url-in-body | ⬜ | — |
| ngpvan | basic-auth | next-url-in-body | ⬜ | — |
| phone2action | basic-auth | next-url-in-body | ⬜ | — |
| quickbase | header-token | none | ⬜ | — |
| redash | header-token (`Key`) | polling | ⬜ | — |
| rockthevote | api-key-param | polling | ⬜ | — |
| scytl | none | none | ⬜ | — |
| shopify | header-token | link-header | ⬜ | — |
| sisense | header-token | none | ⬜ | — |
| turbovote | expiring-token | none | ⬜ | — |
| zoom | oauth2 | cursor | ⬜ | — |
| airtable, alchemer, aws, azure, box, braintree, civis, facebook_ads, geocode, github, google, notifications, salesforce, twilio | — | — | ⛔ | vendor SDK |
| databases, etl, sftp | — | — | ⛔ | non-HTTP |

## The classification

Every HTTP connector falls into one **auth family** and one **pagination
family**. Migrating is mostly a matter of looking up the row and applying the
mapped helper + paginator.

**Auth family → what to use**

| Auth family | Meaning | Use |
|---|---|---|
| basic-auth | user/password or key-as-user tuple | keep `auth=(...)` / `HTTPBasicAuth` |
| header-token | static token in a header | `HeaderTokenAuth(token, header=..., template=...)` |
| expiring-token | token fetched from a login endpoint, expires | `ExpiringTokenAuth(fetch_token)` |
| oauth2 | client-credentials grant | `OAuth2APIConnector` |
| api-key-param | key in the query string | leave it a query param (no auth object) |
| none | no auth (public files) | nothing |

**Pagination family → what to use**

| Pagination family | Meaning | Use |
|---|---|---|
| link-header | next URL in the `Link:` header | `LinkHeaderPaginator(rel="next")` |
| next-url-in-body | next URL in the JSON body | `NextUrlPaginator("dotted.path")` |
| cursor | opaque cursor echoed as a query param | `CursorPaginator(cursor_key, cursor_param)` |
| page-number | incrementing page number | `PageNumberPaginator(page_param, ...)` |
| polling | submit a job, poll until ready | keep the loop connector-side (no paginator) |
| none | single request | nothing |

See the [layer reference](../parsons/utilities/README.md) for the exact
paginator signatures.

## The migration recipe

1. **Look up the connector** in the status tracker (auth family, pagination
   family).
2. **Set auth** on the `APIConnector`: keep the `auth=` tuple for basic-auth;
   use `HeaderTokenAuth` / `ExpiringTokenAuth` for token headers; switch to
   `OAuth2APIConnector` for client-credentials; leave API keys as query params.
   Delete any hand-rolled token-fetch/refresh code the helper now covers.
3. **Replace the pagination loop** with `paginate()`:
   ```python
   rows = []
   for response in self.client.paginate(endpoint, <Paginator>, params=params):
       rows.extend(response.json()[<data key>])
   ```
4. **Swap request calls** to the verb methods where returning a `Response` is
   cleaner (`.json()`), or leave existing `*_request` calls as-is (they still
   work).
5. **Add reliability config** where the connector hand-rolled it: map a manual
   `time.sleep(n)` between calls to `rate_limit_interval=n`; pass `timeout=`
   / `retries=` if the connector wants them ahead of the fleet-wide default flip.
6. **Regression gate:** the connector's **existing** tests must pass
   *unmodified*. If a migration forces you to change an assertion in an existing
   test, that is evidence of an unintended behavior change — stop and
   reconcile.
7. **Add one pagination test**: multiple pages concatenate, and the page /
   cursor / URL advances correctly. Write it the way `write_tests.rst`
   prescribes (see below) — for a paginated GET, register a multi-page
   `requests_mock` response list and assert the concatenated result and that
   the page/cursor/URL advanced.
8. **One connector per PR.** Declare it breaking or non-breaking (it should be
   non-breaking).

**How to write the tests:** follow the canonical
[testing guide](write_tests.rst) — it is the single source of truth for test
structure (plain pytest functions, per-connector ``conftest.py``, canned
payloads under ``data/``) and for the "mock the outermost boundary you don't
own" rule. For the HTTP connectors this migration covers, that boundary is the
`requests_mock` fixture, so a migration's own request/response assertions and
its new pagination test both use `requests_mock`. This guide only adds the two
migration-specific rules above (the regression gate and one pagination test);
everything else about *how* to write the test lives in `write_tests.rst`. If a
connector you are migrating still uses the older `unittest.TestCase` style,
converting it to the standard is the testing effort's job — do not bundle that
conversion into the API-client migration PR (it would collide with that work).

### Worked examples

Two connectors are already migrated on branch `api-connector-refactor` (PR #3)
— read their diffs as templates:

- **freshdesk** (`git show 0fc5faa8d9`) — basic-auth tuple × Link header. A
  regex `while "link" in headers` loop becomes
  `paginate(endpoint, LinkHeaderPaginator())`.
- **quickbooks** (`git show fdd5cd1e40`) — Bearer header × page number. A manual
  `page += 1 while response["more"]` loop becomes
  `paginate(end_point, PageNumberPaginator(more_key="more"))`.

## POC batch

Before the broad waves, this batch migrates one connector from each auth ×
pagination combination, so every shared primitive is proven end-to-end on a
real connector:

| POC | Proves (auth) | Proves (pagination) | Also exercises |
|---|---|---|---|
| freshdesk ✅ | keep basic-auth tuple | `LinkHeaderPaginator` | — |
| quickbooks ✅ | `HeaderTokenAuth` (Bearer) | `PageNumberPaginator(more_key)` | body "more" flag |
| targetsmart ✅ | `HeaderTokenAuth` (custom header) | none | the simplest possible migration |
| crowdtangle ✅ | API key stays a query param | `NextUrlPaginator` (nested) | `rate_limit_interval` |
| actblue ✅ | keep basic-auth tuple | none — polling loop | verb methods around an async job |
| hustle ✅ | `OAuth2APIConnector` | `CursorPaginator` (more_key) | deletes hand-rolled token+refresh |
| pdi ✅ | `ExpiringTokenAuth` | count-driven cursor (kept) | login-body token, expiry refresh |

The POC batch is complete on branch `api-connector-refactor`: 7 connectors
covering all four paginators (`LinkHeaderPaginator`, `PageNumberPaginator`,
`NextUrlPaginator`, `CursorPaginator`), all three auth helpers
(`HeaderTokenAuth`, `ExpiringTokenAuth`, `OAuth2APIConnector`) plus
keep-basic-auth and api-key-in-query, the `rate_limit_interval` config, and the
polling escape hatch. hustle also drove the additive `CursorPaginator.more_key`
stop-flag. Only capitol_canary was held back (see note).

### Per-connector notes (POC batch)

- **capitol_canary** — ⚠️ **held back (a POC finding).** It looks like a clean
  `NextUrlPaginator("pagination.next_url")` case, but its loop actually stops on
  *page fullness* (`count == per_page`), not on `next_url` being absent, and the
  test fixture returns a `next_url` even on the last page. Switching to
  `NextUrlPaginator` would change the stop semantics (and hang that test on the
  fixture). Resolve before migrating: confirm the **live** API sets
  `next_url`/`nextPageLink` to null on the last page — if so, adopt
  `NextUrlPaginator` and make the fixture realistic; if not, keep a page-size
  stop. `NextUrlPaginator` itself is already proven by `crowdtangle`, so this is
  not blocking coverage. (phone2action shares this code and inherits the same
  question.)
- **hustle** — ✅ *done.* Replaced `_get_auth_token` / `_refresh_token`
  with `OAuth2APIConnector` (client-credentials); replace the loop with
  `CursorPaginator("pagination.cursor", "cursor")`, `data_key="items"`.
  Complications this POC surfaces: (1) hustle signals the end via
  `pagination.hasNextPage == "true"` — a **string**, so the migration must add
  a *truthy-aware* stop-flag option to `CursorPaginator` (mirroring
  `PageNumberPaginator.more_key`, but treating `"false"`/`"0"`/`""` as falsy);
  (2) `self.auth_token` must remain equal to the access token (an existing test
  asserts it) — set it from `self.client.token["access_token"]`; (3) hustle's
  `_error_check` treats only 200/201 as success and has a `raise_on_error`
  flag, so route through `client.request()` + the existing `_error_check`, not
  the validating verb methods.
- **pdi** — ✅ *done.* Wrapped the `POST /sessions` login
  (Username/Password/ApiToken → AccessToken + ExpirationDate) in
  `ExpiringTokenAuth.fetch_token`, parsing ExpirationDate for the refresh
  margin. Complication: pdi's `_request` has two pagination modes, and the
  **limit mode uses a variable page size per request**
  (`min(LIMIT_MAX, total_need - len(data))`), which `PageNumberPaginator`
  cannot express. Options: use `PageNumberPaginator` for the unbounded mode and
  keep a small custom loop for the explicit-limit mode, or migrate transport +
  `ExpiringTokenAuth` only and leave the count-driven loop in place.
- **crowdtangle** — the key stays a query param (no auth helper);
  `NextUrlPaginator("result.pagination.nextPage")`; set
  `rate_limit_interval=REQUEST_SLEEP` to replace the manual 10s sleep. Data
  lives under `result.<dynamic-first-key>`.
- **actblue** — swap `post_request` / `get_request` for `post` / `get`
  returning a `Response`; **keep** `poll_for_download_url` (no paginator
  applies); the final CSV is still fetched via `Table.from_csv(download_url)`.
- **targetsmart** — `HeaderTokenAuth(api_key, header="x-api-key", template="{token}")`;
  no pagination. The sibling modules `targetsmart_smartmatch` (S3 upload) and
  `targetsmart_automation` (SFTP) are non-HTTP and out of scope.

## After the POC: the waves

Once the POC batch has merged and the paginator/auth patterns are proven, the
rest migrate in difficulty order, **one connector per PR**:

- **Wave A — existing `APIConnector` users** (easy): they already compose the
  client; the PR just deletes hand-rolled pagination/token/throttle code in
  favor of the shared primitives.
- **Wave B — raw-`requests` connectors** (medium): move onto `APIConnector`;
  some need `**kwargs` passthrough (`files=`, `stream=`).
- **Wave C — hard cases** (minimal ambition): large surfaces (`action_kit`),
  polling models (`redash`), or non-JSON (`scytl`, `mobilecommons` XML) adopt
  the shared transport (timeouts/retries) first, without a full rewrite.

## Reviewer rules

- A migration PR that **forces an assertion change in an existing test** is
  evidence of an unintended behavior change. Investigate before merging.
- **One connector per PR** — never batch.
- Any change to a **shared primitive** (a new paginator option, etc.) is
  additive, lands with its own unit tests in
  `test/test_utilities/test_api_connector_features.py`, and is its own PR ahead
  of the connector that needs it.
- Every PR declares **breaking / non-breaking** (migrations should be
  non-breaking).

## See also

- [`write_tests.rst`](write_tests.rst) — the canonical guide for **how** to
  write connector tests (structure, mocking rule, test data). This migration
  guide defers to it for everything except the two migration-specific rules
  above.
- [`parsons/utilities/README.md`](../parsons/utilities/README.md) — the HTTP layer reference.
- The overall refactor plan (sessions/timeouts/retries, the default-on flips,
  and the full wave sequencing) lives with the effort's design notes.
