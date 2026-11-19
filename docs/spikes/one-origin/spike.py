"""One-origin spike: drives Chromium against the throwaway gateway of up.sh (docs/spikes/one-origin.md).
Usage: python spike.py examples/workflows/expense-approval.json <results.json>"""
import json
import re
import sys
import time
import urllib.error
import urllib.request

from playwright.sync_api import sync_playwright

G = "http://localhost:5990"
OWNER = {"email": "owner@example.com", "password": "SpikePass123"}
EXAMPLE = sys.argv[1]
out = {}
RUN = str(int(time.time()))


def log(key, value):
    out[key] = value
    print(f"{key}: {value}", flush=True)


def rest(page, method, path, body=None):
    return page.evaluate("""async ([method, path, body]) => {
        const r = await fetch('/n8n/rest' + path, {method, headers: {'content-type': 'application/json',
          'browser-id': localStorage.getItem('n8n-browserId') || ''}, body: body ? JSON.stringify(body) : undefined});
        return [r.status, await r.json().catch(() => null)]; }""", [method, path, body])


def post(url, body):
    req = urllib.request.Request(url, json.dumps(body).encode(), {"content-type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:200]


HOOK_FLOW = {"name": "spike webhooks", "settings": {"executionOrder": "v1"}, "connections": {
    "Hook": {"main": [[{"node": "Respond", "type": "main", "index": 0}]]},
    "Respond": {"main": [[{"node": "Wait", "type": "main", "index": 0}]]},
    "Wait": {"main": [[{"node": "Done", "type": "main", "index": 0}]]}},
    "nodes": [
        {"id": "1", "name": "Hook", "type": "n8n-nodes-base.webhook", "typeVersion": 2, "position": [0, 0],
         "webhookId": "hook" + RUN, "parameters": {"path": "spike" + RUN, "httpMethod": "POST", "responseMode": "responseNode"}},
        {"id": "2", "name": "Respond", "type": "n8n-nodes-base.respondToWebhook", "typeVersion": 1.1, "position": [200, 0],
         "parameters": {"respondWith": "json", "responseBody": "={{ JSON.stringify({resume: $execution.resumeUrl}) }}"}},
        {"id": "3", "name": "Wait", "type": "n8n-nodes-base.wait", "typeVersion": 1.1, "position": [400, 0],
         "webhookId": "wait" + RUN, "parameters": {"resume": "webhook", "httpMethod": "POST"}},
        {"id": "4", "name": "Done", "type": "n8n-nodes-base.noOp", "typeVersion": 1, "position": [600, 0], "parameters": {}}]}

with sync_playwright() as p:
    browser = p.chromium.launch()
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    page = ctx.new_page()
    failed = []
    page.on("response", lambda r: r.status >= 400 and failed.append(f"{r.status} {r.url}"))

    # 1. the editor under /n8n/: sign in through its own form
    page.goto(G + "/n8n/signin")
    page.locator("input[type=email]").fill(OWNER["email"])
    page.locator("input[type=password]").fill(OWNER["password"])
    page.keyboard.press("Enter")
    page.wait_for_url(re.compile(r"/n8n/home"), timeout=20000)
    log("1 login lands on", page.url)
    cookie = next(c for c in ctx.cookies() if c["name"] == "n8n-auth")
    log("1 n8n-auth cookie", {k: cookie[k] for k in ("domain", "path", "secure", "httpOnly", "sameSite")})

    # 2. open a workflow with bpmn8n nodes, rename it, save with Ctrl+S
    example = json.load(open(EXAMPLE))
    status, created = rest(page, "POST", "/workflows", {k: example[k] for k in ("name", "nodes", "connections", "settings")})
    wf = created["data"]
    failed.clear()
    page.goto(f"{G}/n8n/workflow/{wf['id']}")
    page.wait_for_selector("[data-test-id=canvas-node]", timeout=30000)
    time.sleep(3)
    log("2 canvas nodes", page.locator("[data-test-id=canvas-node]").count())
    log("2 hook scripts in <head>", page.evaluate("[...document.scripts].map(s => s.src).filter(s => s.includes('bpmn8n'))"))
    log("2 tokens link", page.evaluate("document.querySelector('link[data-bpmn8n-tokens]')?.href"))
    log("2 tokens applied (--b8-canvas)", page.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--b8-canvas').trim()"))
    log("2 failed requests after opening", sorted(set(failed)))
    page.locator("[data-test-id=inline-edit-preview], [data-test-id=workflow-name-input]").first.click()
    page.keyboard.press("ControlOrMeta+a")
    page.keyboard.type("Spike renamed")
    page.keyboard.press("Enter")
    time.sleep(1)
    page.keyboard.press("ControlOrMeta+s")
    time.sleep(2)
    status, saved = rest(page, "GET", f"/workflows/{wf['id']}")
    log("2 saved name (REST)", saved["data"]["name"])

    # 3. production and waiting webhooks at the root paths WEBHOOK_URL gives them
    status, created = rest(page, "POST", "/workflows", HOOK_FLOW)
    hook = created["data"]
    log("3 activate", rest(page, "POST", f"/workflows/{hook['id']}/activate", {"versionId": hook["versionId"]})[0])
    time.sleep(3)
    status, settings = rest(page, "GET", "/settings")
    log("3 urlBaseWebhook / urlBaseEditor", [settings["data"].get("urlBaseWebhook"), settings["data"].get("urlBaseEditor")])
    code, body = post(G + "/webhook/spike" + RUN, {"hello": 1})
    log("3 POST /webhook/spike", [code, body])
    time.sleep(2)  # the Respond node answers before the Wait node parks the execution
    code, resumed = post(body["resume"], {"approved": True})
    log("3 POST resumeUrl", [code, resumed])
    time.sleep(2)
    status, runs = rest(page, "GET", f"/executions?filter={json.dumps({'workflowId': hook['id']})}")
    log("3 execution status", [r["status"] for r in runs["data"]["results"]])
    code, _ = post(G + "/n8n/webhook/spike" + RUN, {"hello": 2})
    log("3 POST /n8n/webhook/spike (prefixed path works too)", code)

    # 4. Django on the same origin reads the n8n session (evaluation_read_auth), no CORS
    read = page.evaluate("""async id => { const r = await fetch('/api/evaluations/' + id + '/context',
        {headers: {'browser-id': localStorage.getItem('n8n-browserId')}});
        return [r.status, r.headers.get('access-control-allow-origin')]; }""", wf["id"])
    log("4 /api/evaluations/<id>/context with the n8n cookie [status, ACAO]", read)
    anon = browser.new_context().request.get(f"{G}/api/evaluations/{wf['id']}/context")
    log("4 same read without the cookie", anon.status)

    # 5. preview on its own host, framed by a Django page and by the editor; cookies never cross hosts
    for where in ("/login/", f"/n8n/workflow/{wf['id']}"):
        page.goto(G + where)
        time.sleep(3)
        page.evaluate("""() => { const f = document.createElement('iframe'); f.setAttribute('credentialless', '');
            f.src = 'http://preview.localhost:5990/workflows/demo?theme=dark'; document.body.append(f); }""")
        time.sleep(8)
        inner = page.frame_locator("iframe").last
        log(f"5 preview framed by {where}: app mounted, secure-cookie warning",
            [inner.locator("#app").count(), inner.locator("text=secure cookie").count()])
    log("5 n8n-auth after preview on its own host", bool([c for c in ctx.cookies() if c["name"] == "n8n-auth"]))
    direct = ctx.new_page()
    direct.goto("http://preview.localhost:5990/workflows/demo")
    log("5 Chromium resolves preview.localhost (no /etc/hosts)", [direct.url, direct.title()])
    preview = ctx.new_page()
    preview.goto("http://localhost:5993/workflows/demo")  # the preview on the editor's host name (cookies ignore ports)
    time.sleep(6)
    log("5 n8n-auth after preview on the same host name", bool([c for c in ctx.cookies() if c["name"] == "n8n-auth" and c["domain"] == "localhost"]))

    # 6. identity from the proxy: a header the gateway would set after Django's forward_auth
    fresh = browser.new_context(extra_http_headers={"X-Forwarded-Email": OWNER["email"]})
    fp = fresh.new_page()
    fp.goto(G + "/n8n/home/workflows")
    time.sleep(5)
    log("6 with X-Forwarded-Email lands on", fp.url)
    log("6 workflows listed", fp.locator("[data-test-id=resources-list-item], [data-test-id=resources-list-item-workflow]").count())
    plain = browser.new_context().new_page()
    plain.goto(G + "/n8n/home/workflows")
    time.sleep(4)
    log("6 without the header: sign-in form shown", plain.locator("input[type=password]").count() == 1)
    browser.close()
json.dump(out, open(sys.argv[2], "w"), indent=1)
