# Browser end-to-end tests

Real-browser tests of the dashboard against the real API, the real worker, real PostgreSQL and Redis,
and a **fake Shopify served over HTTP** (`backend/tests/shopify/http_server.py`: the same object the
unit tests use, with real OAuth redirects). One command starts everything in an isolated database,
runs the suites and tears it all down:

```bash
docker compose up -d db redis        # from the repository root (once)
cd widget && npm ci && npm run build # once: writes frontend/public/widget.js
cd frontend && npm ci                # once
cd backend && python -m venv .venv && .venv/Scripts/pip install -r requirements-dev.txt   # once
cd e2e && npm ci                     # once
npm test                             # offline suites: auth, security, layout, shopify
npm run test:full                    # + knowledge and chat (they crawl example.com and quotes.toscrape.com)
node run.mjs auth shopify            # only the named suites
node run.mjs --keep                  # leave the stack running afterwards, for debugging
```

## What it does

1. Checks prerequisites and that ports 3000, 8000, 9400 (and 5173 for `chat`) are free, so it can
   never test something other than what it started.
2. Creates the database `bizflow_e2e` (and uses Redis db 14), applies the real Alembic migrations.
   **Your development database is never touched.** Both are removed at the end.
3. Starts the fake Shopify, the API, the worker and the dashboard (`npm run dev`: production mode
   correctly refuses the `http://` redirect the fake uses). Every secret is generated for the run and
   lives only in the child processes' environment.
4. Runs each suite (`*.spec.js`) as its own process and prints a PASS/FAIL summary; the exit status is
   non-zero if any suite failed. Logs of each service and screenshots go to
   `e2e/artifacts/<timestamp>/` (git-ignored; delete them when you are done).

## Suites

| Suite | Needs internet | Covers |
| --- | :-: | --- |
| `auth` | no | Sign-up and login, validation errors, normalisation, onboarding, session cookie flags, no token or backend URL in the page, two tenants cannot see each other |
| `security` | no | Response headers (no framing, `nosniff`, referrer policy, CSP, no `X-Powered-By`), refusal to render in a foreign iframe, cookie `HttpOnly`/`SameSite`/expiry, nothing in storage, a forged cookie opens nothing |
| `layout` | no | No horizontal scrolling or overflow on a 390 px phone on every dashboard page |
| `shopify` | no | Connect (valid, declined, forged and replayed callbacks), live sync progress, signed/duplicate/forged webhooks applied by the worker, member vs owner, two tenants, disconnect purge, reconnect, revoked access, a scan of everything the browser received for secrets and customer data |
| `knowledge` | yes | Adding and crawling a real site, rejected URLs, multi-page crawl, blocked targets |
| `chat` | yes | The embeddable widget on a third-party page, grounded answers, origin allow-list, rate limit, outage messages |

## Browser

No download is needed if Chrome or Edge is installed (`playwright-core` drives it). Order tried:
`E2E_BROWSER_CHANNEL` if set, bundled Chromium (`npx playwright install chromium`), Chrome, Edge.

## Running a suite against a stack you started yourself

Every address is configurable, so the same scripts work against any environment you control:
`E2E_BASE`, `E2E_API`, `E2E_FAKE`, `E2E_SHOP`, `E2E_SHOPIFY_CLIENT_SECRET`, `E2E_ARTIFACTS`,
`E2E_DB_CONTAINER`, `E2E_DB_NAME` (the Shopify suite asserts on database contents with `psql` inside
that container). Never point them at a database or Shopify store that holds real data.

## No credentials, no real customer data

Nothing here needs a Shopify or AI key. The fake store's customers are synthetic
(`shopperN@private-example.test`, `+1555000NNNN`). The Shopify suite checks that none of that data,
and none of the generated tokens, ever reaches the browser.
