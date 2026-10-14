# All-in-one image: FlareSolverr (Chrome) + API
# Compatible with Render, Railway and other Docker hosts.

FROM ghcr.io/flaresolverr/flaresolverr:latest

# Ensure build commands have permission to create the virtual environment.
USER root

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PATH="/srv/api/.venv/bin:$PATH"

WORKDIR /srv/api

# Install Python dependencies.
COPY requirements.txt ./

RUN /usr/local/bin/python -m venv /srv/api/.venv \
    && /srv/api/.venv/bin/python -m pip install --upgrade pip \
    && /srv/api/.venv/bin/python -m pip install -r requirements.txt

# Copy API source code.
COPY . .

# Ensure the startup script is executable.
RUN chmod +x /srv/api/start.sh

EXPOSE 8000

ENTRYPOINT ["/srv/api/start.sh"]
