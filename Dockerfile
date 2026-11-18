# syntax=docker/dockerfile:1
# bpmn8n's web service (Django + gunicorn) for production: wheels built in one stage, a slim runtime without
# compilers or pip downloads, static files collected at build time, no --reload, a non-root user.
#   docker build -t bpmn8n-web .      then docker-compose.prod.yml runs it
FROM python:3.12-slim AS wheels
WORKDIR /src
# requirements.local.txt (git-ignored): the packages of local projects, when there is one
COPY requirements*.txt ./
RUN pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt \
    $( [ -f requirements.local.txt ] && echo -r requirements.local.txt )

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DEBUG=0 WEB_WORKERS=4
RUN --mount=type=bind,from=wheels,source=/wheels,target=/wheels \
    pip install --no-cache-dir --no-index /wheels/*.whl \
    && useradd --create-home --uid 1000 bpmn8n
WORKDIR /app
COPY . .
# DEBUG=0 settings refuse to load without these: placeholders for this one command, not kept in the image
RUN DEBUG=0 SECRET_KEY=collectstatic BPMN8N_API_TOKEN=collectstatic ALLOWED_HOSTS=localhost \
    python manage.py collectstatic --noinput -v 0 \
    && chown -R bpmn8n:bpmn8n projects  # the code stays root's; projects/ takes Metrics samples and Studio builds
USER bpmn8n
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health', timeout=4)"
# ponytail: migrations run at boot, fine for one web container; with several replicas run them once
# (docker compose run --rm web python manage.py migrate) and start gunicorn alone
CMD ["sh", "-c", "python manage.py migrate --noinput && exec gunicorn bpmn8n.wsgi -b 0.0.0.0:8000 --workers ${WEB_WORKERS} --threads 16 --backlog 2048 --keep-alive 0 --graceful-timeout 900 --pid /tmp/gunicorn.pid --access-logfile -"]
