FROM ghcr.io/flaresolverr/flaresolverr:latest

USER root

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PATH="/srv/api/.venv/bin:$PATH" \
    FLARESOLVERR_URL="http://127.0.0.1:8191/v1"

WORKDIR /srv/api

COPY requirements.txt ./

RUN /usr/local/bin/python -m venv /srv/api/.venv \
    && /srv/api/.venv/bin/python -m pip install --upgrade pip \
    && /srv/api/.venv/bin/python -m pip install -r requirements.txt

COPY . .

EXPOSE 8000 8191

CMD ["/srv/api/.venv/bin/uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
