#!/usr/bin/env bash
# Starts bpmn8n: the bootstrap of `python -m bpmn8n` (bpmn8n/cli.py), which does the work. It makes sure there is
# a Python with bpmn8n's packages (.venv), then hands over:
#
#   ./start.sh          python -m bpmn8n up       (safe to re-run: it rebuilds and reloads the nodes)
#   ./start.sh stop     python -m bpmn8n down     (data is kept)
#   ./start.sh status | logs | export | import | doctor | deploy | diff    the same command
#
# n8n login used to create the bpmn8n credential: N8N_EMAIL / N8N_PASSWORD (the environment, else .env).
set -euo pipefail
cd "$(dirname "$0")"

PY=.venv/bin/python
if ! "$PY" -c "import django, pydantic" 2>/dev/null; then
  echo "… creating .venv with bpmn8n's packages (first run only)"
  if command -v uv >/dev/null; then
    [ -x "$PY" ] || uv venv -q --python 3.12 .venv
    uv pip install -q --python "$PY" -r requirements.txt
  else
    [ -x "$PY" ] || python3 -m venv .venv
    "$PY" -m pip install -q -r requirements.txt
  fi
fi

case "${1:-start}" in
  start) set -- up ;;
  stop) set -- down ;;
esac
exec "$PY" -m bpmn8n "$@"
