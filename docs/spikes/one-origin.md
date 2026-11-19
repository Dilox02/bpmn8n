# Spike: one origin and one identity (PLAN B1, B2)

2026-11-19, n8n 2.42.3, Caddy 2.11.9, Chromium (Playwright). Throwaway containers only (`b8spike-*`): n8n with
`N8N_PATH=/n8n/`, `N8N_EDITOR_BASE_URL=http://localhost:5990/n8n/` and `N8N_WEBHOOK_URL=http://localhost:5990/`, its
preview, `deploy/Caddyfile` on `127.0.0.1:5990`, and Django from the checkout on `127.0.0.1:5991` (SQLite, an API token,
`PUBLIC_URL=http://localhost:5990`). The tools are in [`one-origin/`](one-origin/).

## Answers

| Question | Answer | Evidence |
|---|---|---|
| Does the editor work under `/n8n/`? | **Yes**, once the proxy strips the prefix: n8n puts `N8N_PATH` only in the pages it serves (`/{{BASE_PATH}}/` in `index.html` and the bundles, `window.BASE_PATH`); its routes stay at its root (`/rest`, `/assets`, `/icons`) | sign-in through the form lands on `/n8n/home/workflows`; a workflow with 7 bpmn8n nodes opens; rename + Ctrl+S is read back through `/n8n/rest/workflows/<id>`; the push websocket `ws://…/n8n/rest/push` connects (no "Connection lost") |
| Webhooks and waiting webhooks? | **Yes**, at the gateway's root (`N8N_WEBHOOK_URL`, which replaces the deprecated `WEBHOOK_URL`) | `POST /webhook/<path>` 200; the Wait node's `$execution.resumeUrl` is `http://localhost:5990/webhook-waiting/<id>?signature=…`; `POST` on it 200 and the execution ends `success`; `/n8n/webhook/<path>` works too |
| Do the hooks load? What must change? | **They load** with `EXTERNAL_FRONTEND_HOOKS_URLS` prefixed (`/n8n/static/bpmn8n-*.js`: n8n injects the URLs as written, with no `<base>`). Before the change two requests failed: `404 /static/bpmn8n-tokens.css` and `404 /icons/CUSTOM/nodes/icons/bpmn/events/start-event-message.svg` (they went to Django). `executions.js` called `/rest` and `evaluations.js` linked `/workflow/…` the same way | after: no failed request, `--b8-canvas` applied, tokens at `/n8n/static/bpmn8n-tokens.css`. The hooks now take n8n's `window.BASE_PATH`, else their own URL (`n8n-editor/test/base.test.mjs`) |
| Does the preview still clear the editor's cookie on a shared host? | **Yes**: the editor's `n8n-auth` cookie (`Path=/`, host `localhost`) is gone after opening the preview on `localhost:5993` (cookies ignore ports). On `preview.localhost` it stays | the preview framed by a Django page and by the editor: n8n's `#app` mounted, cookie kept |
| Preview host over plain HTTP | n8n's editor refuses to run with a Secure cookie on plain HTTP unless the host is `localhost`/`127.0.0.1` (and always on Safari): it writes "Your n8n server is configured to use a secure cookie…" | `preview.localhost` showed that page until the preview ran with `N8N_SECURE_COOKIE=false` (it signs nobody in). The preview's `frame-ancestors` must name the gateway origin (Caddy sets it) |
| `*.localhost` in Chromium without `/etc/hosts`? | **Yes** | `page.goto("http://preview.localhost:5990/workflows/demo")` loads n8n. Safari and Firefox not tested |
| Same-origin reads from Django | **Work** with the n8n cookie and need no CORS | `/api/evaluations/<id>/context` 200 with the cookie, 401 without, no `Access-Control-Allow-Origin` |
| Identity from a proxy (B2) | **Not supported by Community Edition.** Its SSO (OIDC, SAML, LDAP) is licensed (`modules/sso-oidc`: `licenseFlag: 'feat:oidc'`) and there is no trusted-header option. It is *possible* through `EXTERNAL_HOOK_FILES`: an `n8n.ready` hook gets the Express app and n8n's DI container and can issue n8n's own cookie for an email the proxy asserts ([`forward-auth.js`](one-origin/forward-auth.js)) | with `X-Forwarded-Email: owner@example.com` a fresh browser opens `/n8n/home/workflows` signed in and lists the workflows; without it, the sign-in form |

**Decision for B2.** The hook works but depends on n8n's private internals (`dist/auth/auth.service.js`, the DI
container, Express 5's router stack), it would make n8n trust a header (anyone who reaches n8n's port directly could
send it), and it replicates a paid feature: not adopted. The fallback is built: two logins, one account.
`manage.py n8n_sync_owner <user>` (or `bpmn8n.clients.n8n.sync_account`, for the CLI) gives a staff user the same email
and password in n8n through its REST API: owner setup on a fresh n8n; otherwise the owner signs in and invites the
user as a member (`/rest/invitations`, then `/rest/invitations/accept` with the link's token) or resets its password
(`/rest/users/<id>/password-reset-link`, then `/rest/change-password`; this also completes an invite left pending);
the owner's own password through `/rest/me/password`. All four paths were run against a throwaway n8n (a member signs
in with the new password, the old one is refused; a fresh n8n makes the user `global:owner`). n8n rate-limits
`/rest/login` (HTTP 429 after a few attempts a minute) and asks for a last name (the command sends `-` when there is
none).

## Repeat

```bash
sh docs/spikes/one-origin/up.sh                    # HOOK_FILE=1 adds forward-auth.js; PREVIEW_SECURE_COOKIE=false
DB_PATH=/tmp/spike.sqlite3 python manage.py migrate
DB_PATH=/tmp/spike.sqlite3 BPMN8N_API_TOKEN=t N8N_URL=http://127.0.0.1:5992 PUBLIC_URL=http://localhost:5990 \
  python manage.py runserver 127.0.0.1:5991 --noreload &
python docs/spikes/one-origin/spike.py examples/workflows/expense-approval.json /tmp/one-origin.json
docker run --rm -v "$PWD/deploy:/etc/caddy:ro" caddy:2.11-alpine caddy validate --config /etc/caddy/Caddyfile
N8N_URL=http://127.0.0.1:5992 N8N_EMAIL=owner@example.com N8N_PASSWORD=SpikePass123 \
  DB_PATH=/tmp/spike.sqlite3 python manage.py n8n_sync_owner <staff user>
docker rm -f b8spike-n8n b8spike-preview b8spike-caddy && docker network rm b8spike-net
```
