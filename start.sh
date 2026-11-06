#!/usr/bin/env bash
# Starts bpmn8n: builds the n8n BPMN nodes, starts the models (Laya decisions + GLiNER2.5
# extraction: natively on a Mac, Laya on the Apple GPU; in CPU containers elsewhere), then
# Django (web) and n8n in Docker.
#
#   ./start.sh          start everything (safe to re-run: it rebuilds and reloads the nodes)
#   ./start.sh stop     stop everything (data is kept)
#   ./start.sh status   what is running
#   ./start.sh logs     follow the logs
#   ./start.sh export   n8n workflows -> workflows/*.json (version them with git: diffs, history)
#   ./start.sh import   workflows/*.json -> n8n (published workflows stay published)
#
# n8n login used to create the bpmn8n credential: N8N_EMAIL / N8N_PASSWORD (the environment, else .env).
set -euo pipefail
cd "$(dirname "$0")"

env_get() { grep -s "^$1=" .env | cut -d= -f2- || true; }

N8N_EMAIL=${N8N_EMAIL:-$(env_get N8N_EMAIL)}
N8N_PASSWORD=${N8N_PASSWORD:-$(env_get N8N_PASSWORD)}
LOGS=.logs
MODELS_PID=$LOGS/models.pid
MODELS=http://127.0.0.1:8001
VENV=.venv-models
MODEL_PACKAGES=(laya-mlx "gliner2[local]" protobuf sentencepiece tiktoken pyobjc-framework-Vision)
mkdir -p "$LOGS"

apple_silicon() { [[ "$(uname -s)" == Darwin && "$(uname -m)" == arm64 ]]; }

need() { command -v "$1" >/dev/null || { echo "✗ missing $1: $2"; exit 1; }; }

wait_for() {  # name url [seconds]
  printf '… %s ' "$1"
  for ((i = 0; i < ${3:-180}; i += 2)); do
    curl -sf -o /dev/null "$2" && { echo ✓; return; }
    sleep 2
  done
  echo "✗ not ready after ${3:-180}s ($2)"; exit 1
}

build_nodes() {
  echo "… building the n8n BPMN nodes"
  (cd n8n-nodes-bpmn8n && { [ -d node_modules ] || npm install --ignore-scripts --silent; } && npm run build --silent >/dev/null)
}

start_models() {
  if curl -sf -o /dev/null "$MODELS/health"; then echo "✓ models server already running"; return; fi
  if ! "$VENV/bin/python" -c "import laya_mlx, gliner2, Vision" 2>/dev/null; then
    need uv "https://docs.astral.sh/uv/ (brew install uv)"
    echo "… installing laya-mlx, GLiNER2.5 and Vision for OCR (first run only)"
    [ -x "$VENV/bin/python" ] || uv venv -q --python 3.12 "$VENV"
    uv pip install -q --python "$VENV/bin/python" "${MODEL_PACKAGES[@]}"
  fi
  nohup "$VENV/bin/python" tools/models_server.py >"$LOGS/models.log" 2>&1 &
  echo $! >"$MODELS_PID"
  wait_for "models server (Laya on the Apple GPU + GLiNER2.5; the first run downloads the weights)" "$MODELS/health" 900
}

ensure_n8n_database() {  # n8n and bpmn8n share the Postgres server, each with its own database
  docker compose up -d --wait postgres >/dev/null
  docker compose exec -T postgres psql -U bpmn8n -d bpmn8n -tAc "SELECT 1 FROM pg_database WHERE datname = 'n8n'" |
    grep -q 1 || docker compose exec -T postgres createdb -U bpmn8n n8n
}

JAR=$LOGS/n8n.cookies

n8n_login() {
  [ -n "$N8N_EMAIL" ] || { echo "! n8n: create the owner account at http://localhost:5678, then: N8N_EMAIL=... N8N_PASSWORD=... ./start.sh"; return 1; }
  local login="{\"emailOrLdapLoginId\":\"$N8N_EMAIL\",\"password\":\"$N8N_PASSWORD\"}"
  curl -sf -o /dev/null -c "$JAR" -H 'content-type: application/json' -d "$login" http://localhost:5678/rest/login && return
  echo "! n8n: create the owner account at http://localhost:5678, then re-run: N8N_EMAIL=... N8N_PASSWORD=... ./start.sh"
  return 1
}

env_set() {  # name value: replaces the line in .env
  { grep -sv "^$1=" .env || true; echo "$1=$2"; } >.env.tmp && mv .env.tmp .env
}

write_env() {  # .env: every later `docker compose ...` (not only this script) gets the same settings
  local key credential; key=$(env_get N8N_API_KEY); credential=$(env_get N8N_CREDENTIAL)
  : >.env
  [ -z "$N8N_EMAIL" ] || printf 'N8N_EMAIL=%s\nN8N_PASSWORD=%s\n' "$N8N_EMAIL" "$N8N_PASSWORD" >>.env
  if apple_silicon; then  # the models run on the host
    printf 'LAYA_URL=%s\nEXTRACTOR_URL=%s\nOCR_URL=%s\n' http://host.docker.internal:8001/v1/systemone \
      http://host.docker.internal:8001/v1/extract http://host.docker.internal:8001/v1/ocr >>.env
  fi
  [ -z "$key" ] || echo "N8N_API_KEY=$key" >>.env
  [ -z "$credential" ] || echo "N8N_CREDENTIAL=$credential" >>.env
}

ensure_api_key() {  # n8n API key: process mining reads workflows, Studio creates them (never activates) and
  local key; key=$(env_get N8N_API_KEY)  # gives their LLM agents the credential of admin → Settings' LLM
  [ -n "$key" ] && curl -sf -o /dev/null -H "X-N8N-API-KEY: $key" "http://localhost:5678/api/v1/workflows?limit=1" &&
    curl -sf -o /dev/null -H "X-N8N-API-KEY: $key" "http://localhost:5678/api/v1/credentials?limit=1" &&
    [ "$(curl -s -o /dev/null -w '%{http_code}' -X PUT -H "X-N8N-API-KEY: $key" -H 'content-type: application/json' \
      -d '{}' "http://localhost:5678/api/v1/workflows/0")" != 403 ] && return  # a key from before Studio updated workflows
  local old; old=$(curl -sf -b "$JAR" http://localhost:5678/rest/api-keys |  # its label must be free
    json_field 'next((k["id"] for k in d["items"] if k["label"] == "bpmn8n"), "")')
  [ -z "$old" ] || curl -sf -o /dev/null -X DELETE -b "$JAR" "http://localhost:5678/rest/api-keys/$old"
  key=$(curl -sf -b "$JAR" -H 'content-type: application/json' http://localhost:5678/rest/api-keys \
    -d '{"label":"bpmn8n","scopes":["workflow:read","workflow:list","workflow:create","workflow:update","credential:list","credential:create","credential:update"],"expiresAt":null}' |
    sed -n 's/.*"rawApiKey":"\([^"]*\)".*/\1/p')
  [ -n "$key" ] || { echo "! n8n API key not created: conformance checking and Studio's import stay off"; return; }
  env_set N8N_API_KEY "$key"
  RESTART_WEB=1
  echo "✓ n8n API key created (workflows: read, list, create, update; credentials: list, create, update)"
}

ensure_credential() {  # the n8n credential the BPMN nodes use to reach bpmn8n; its id goes to .env for Studio
  local id; id=$(curl -sf -b "$JAR" http://localhost:5678/rest/credentials |
    json_field 'next((c["id"] for c in d if c["type"] == "bpmn8nApi"), "")')
  if [ -z "$id" ]; then
    id=$(curl -sf -b "$JAR" -H 'content-type: application/json' http://localhost:5678/rest/credentials \
      -d "{\"name\":\"bpmn8n (web:8000)\",\"type\":\"bpmn8nApi\",\"data\":{\"baseUrl\":\"http://web:8000\",\"apiToken\":\"${BPMN8N_API_TOKEN:-}\"}}" |
      json_field 'd["id"]') && echo "✓ n8n credential 'bpmn8n (web:8000)' created"
  fi
  [ -z "$id" ] || [ "$id" = "$(env_get N8N_CREDENTIAL)" ] || { env_set N8N_CREDENTIAL "$id"; RESTART_WEB=1; }
}

start() {
  need docker "https://docs.docker.com/get-docker/"
  need npm "https://nodejs.org/"
  docker info >/dev/null 2>&1 || { echo "✗ Docker is not running"; exit 1; }
  build_nodes
  ensure_n8n_database
  write_env
  if apple_silicon; then
    start_models
    docker compose up -d web n8n n8n-worker n8n-preview searxng
  else
    docker compose --profile cpu-models up -d  # every service, n8n-preview included
    echo "  Laya and GLiNER run on CPU in Docker; the first start downloads torch and the weights (minutes)"
  fi
  docker compose restart n8n n8n-worker n8n-preview >/dev/null  # load the freshly built nodes
  wait_for "Django (web)" http://localhost:8000/api/openapi.json 600
  docker compose exec -T web python manage.py migrate --noinput >/dev/null  # also when web was already up
  wait_for "n8n" http://localhost:5678/rest/settings 300  # /healthz answers before the REST API
  RESTART_WEB=
  if n8n_login; then
    ensure_credential
    ensure_api_key
  fi
  if [ -n "$RESTART_WEB" ]; then  # web reads .env at start
    docker compose up -d web >/dev/null
    wait_for "Django (web) with the n8n settings" http://localhost:8000/api/openapi.json 600
  fi
  cat <<INFO

bpmn8n is up
  n8n editor      http://localhost:5678         ($N8N_EMAIL)
  docs            http://localhost:8000/docs/   (the framework, and your tasks live)
  inbox           http://localhost:8000/inbox/
  studio          http://localhost:8000/studio/ (design a process with an LLM: admin → Settings first)
  activities      http://localhost:8000/activities/ (every @task, with its decorator and fields)
  API + Swagger   http://localhost:8000/api/docs
  process mining  http://localhost:8000/api/processmining   (all of it: python manage.py mine)
  training data   http://localhost:8000/api/datasets/card
  example flows   examples/workflows/*.json   (n8n: ⋯ → Import from File)
  stop            ./start.sh stop
INFO
}

stop() {
  docker compose --profile cpu-models stop
  if [ -f "$MODELS_PID" ] && kill "$(cat "$MODELS_PID")" 2>/dev/null; then echo "✓ models server stopped"; fi
  pkill -f tools/models_server.py 2>/dev/null || true
  rm -f "$MODELS_PID"
}

status() {
  docker compose --profile cpu-models ps
  for check in "models server|$MODELS/health" "Django|http://localhost:8000/api/openapi.json" "n8n|http://localhost:5678/healthz"; do
    curl -sf -o /dev/null "${check#*|}" && echo "✓ ${check%%|*}" || echo "✗ ${check%%|*}"
  done
}

logs() {
  [ -f "$LOGS/models.log" ] && tail -n 5 "$LOGS/models.log"
  docker compose logs -f --tail 20 web n8n
}

export_workflows() {  # n8n's own CLI: the open alternative to its paid source control
  docker compose exec -T n8n sh -c 'rm -rf /tmp/workflows && n8n export:workflow --all --separate --pretty --output=/tmp/workflows/' >/dev/null
  mkdir -p workflows && docker compose cp n8n:/tmp/workflows/. workflows/ 2>/dev/null
  echo "✓ $(ls workflows | wc -l | tr -d ' ') workflows in workflows/"
}

json_field() { python3 -c "import json, sys; d = json.load(sys.stdin)['data']; print($1)"; }

import_workflows() {  # n8n's import unpublishes workflows: the ones published before are published again
  n8n_login
  local id version published
  published=$(curl -sf -b "$JAR" http://localhost:5678/rest/workflows | json_field '" ".join(w["id"] for w in d if w["active"])')
  docker compose cp workflows/. n8n:/tmp/workflows/ 2>/dev/null
  docker compose exec -T n8n n8n import:workflow --separate --input=/tmp/workflows/
  for id in $published; do
    version=$(curl -sf -b "$JAR" "http://localhost:5678/rest/workflows/$id" | json_field 'd["versionId"]')
    curl -sf -o /dev/null -b "$JAR" -H 'content-type: application/json' -d "{\"versionId\":\"$version\"}" \
      "http://localhost:5678/rest/workflows/$id/activate" && echo "✓ published again: $id"
  done
}

case "${1:-start}" in
  start | stop | status | logs) "${1:-start}" ;;
  export | import) "${1}_workflows" ;;
  *) echo "usage: $0 [start|stop|status|logs|export|import]"; exit 1 ;;
esac
