# All-in-one image: FlareSolverr (Chrome) + the API in ONE container.
# Works on Render / Railway / any single-container host. No cookies needed.
FROM ghcr.io/flaresolverr/flaresolverr:latest

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv/api
COPY requirements.txt ./

# Separate venv so we never clash with FlareSolverr's own Python packages
RUN /usr/local/bin/python -m venv /opt/api-venv \
    && /opt/api-venv/bin/pip install --upgrade pip \
    && /opt/api-venv/bin/pip install -r requirements.txt

COPY . .
RUN chmod +x start.sh

EXPOSE 8000
ENTRYPOINT ["/srv/api/start.sh"]
