#!/bin/sh
# One-origin spike: n8n under /n8n/, its preview on preview.localhost, Django from the checkout, Caddy in front.
# From the repository root: sh docs/spikes/one-origin/up.sh (HOOK_FILE=1: the forward-auth hook; PREVIEW_SECURE_COOKIE=false)
# Down: docker rm -f b8spike-n8n b8spike-preview b8spike-caddy && docker network rm b8spike-net
set -e
W=$(pwd); S=$W/$(dirname "$0"); O=http://localhost:5990
ST=/usr/local/lib/node_modules/n8n/node_modules/n8n-editor-ui/dist/static
NODES=${NODES:-$W/n8n-nodes-bpmn8n/dist}
docker network inspect b8spike-net >/dev/null 2>&1 || docker network create b8spike-net >/dev/null
hooks() { for f in "$@"; do printf ' -v %s/n8n-editor/%s:%s/bpmn8n-%s:ro' "$W" "${f%%=*}" "$ST" "${f#*=}"; done; }
# shellcheck disable=SC2046
docker run -d --rm --name b8spike-n8n --network b8spike-net --network-alias n8n -p 127.0.0.1:5992:5678 \
  -e N8N_PATH=/n8n/ -e N8N_EDITOR_BASE_URL=$O/n8n/ -e N8N_WEBHOOK_URL=$O/ \
  -e N8N_CUSTOM_EXTENSIONS=/bpmn8n-nodes -e N8N_PERSONALIZATION_ENABLED=false -e N8N_DIAGNOSTICS_ENABLED=false \
  ${HOOK_FILE:+-e EXTERNAL_HOOK_FILES=/spike/forward-auth.js} \
  -e "EXTERNAL_FRONTEND_HOOKS_URLS=/n8n/static/bpmn8n-bpmn.js?v=7;/n8n/static/bpmn8n-panel.js?v=5&bpmn8n=$O;/n8n/static/bpmn8n-evaluations.js?v=9&bpmn8n=$O;/n8n/static/bpmn8n-executions.js?v=7&bpmn8n=$O&preview=http://preview.localhost:5990;/n8n/static/bpmn8n-insights.js?v=2&bpmn8n=$O" \
  -v "$S":/spike:ro -v "$NODES":/bpmn8n-nodes:ro -v "$W/bpmn8n/static/bpmn8n/tokens.css:$ST/bpmn8n-tokens.css:ro" \
  $(hooks bpmn.js=bpmn.js panel.js=panel.js evaluations.js=evaluations.js evaluations.css=evaluations.css \
          live.mjs=live.mjs executions.js=executions.js executions.css=executions.css insights.js=insights.js) \
  docker.n8n.io/n8nio/n8n:2.42.3 >/dev/null
# shellcheck disable=SC2046
docker run -d --rm --name b8spike-preview --network b8spike-net --network-alias n8n-preview -p 127.0.0.1:5993:5678 \
  -e N8N_PREVIEW_MODE=true ${PREVIEW_SECURE_COOKIE:+-e N8N_SECURE_COOKIE=$PREVIEW_SECURE_COOKIE} -e N8N_CUSTOM_EXTENSIONS=/bpmn8n-nodes -e N8N_PERSONALIZATION_ENABLED=false \
  -e N8N_DIAGNOSTICS_ENABLED=false -e "N8N_CONTENT_SECURITY_POLICY={\"frame-ancestors\": [\"'self'\", \"http://localhost:5678\"]}" \
  -e "EXTERNAL_FRONTEND_HOOKS_URLS=/static/bpmn8n-bpmn.js?v=6;/static/bpmn8n-theme.js?v=11" \
  -v "$NODES":/bpmn8n-nodes:ro -v "$W/bpmn8n/static/bpmn8n/tokens.css:$ST/bpmn8n-tokens.css:ro" \
  $(hooks bpmn.js=bpmn.js preview.js=theme.js) docker.n8n.io/n8nio/n8n:2.42.3 >/dev/null
docker run -d --rm --name b8spike-caddy --network b8spike-net -p 127.0.0.1:5990:80 \
  -e WEB_UPSTREAM=host.docker.internal:5991 -e BPMN8N_PUBLIC_URL=$O -v "$W/deploy:/etc/caddy:ro" caddy:2.11-alpine >/dev/null
until docker logs b8spike-n8n 2>&1 | grep -q "Editor is now accessible"; do sleep 2; done
until curl -sf -o /dev/null -X POST -H 'content-type: application/json' http://127.0.0.1:5992/rest/owner/setup \
  -d '{"email":"owner@example.com","firstName":"Spike","lastName":"Owner","password":"SpikePass123"}'; do sleep 2; done
echo "up: $O (Django from $W must listen on 127.0.0.1:5991)"
