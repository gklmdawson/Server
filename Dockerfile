# Data Intake Coordinator — one container: job queue + API + web UI.
# Built for the UGREEN NAS (x86_64) or any Docker host on the LAN:
#
#   docker compose up -d --build          (see docker-compose.yml / DEPLOY.md)
#
# Stage 1 builds the React UI so the runtime image needs no Node, and no
# machine in the shop ever needs Node installed.

FROM node:22-alpine AS webui
WORKDIR /build
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM python:3.12-slim
WORKDIR /app

# exiftool: the NAS helper's RTK-coverage scan (Perl, cross-platform) — this is
# the Linux replacement for the bundled Windows exiftool.exe.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libimage-exiftool-perl \
    && rm -rf /var/lib/apt/lists/*

# Install the coordinator package (server deps only — no pywinauto/Qt here).
COPY pyproject.toml README.md ./
COPY coordinator/ coordinator/
COPY shared/ shared/
COPY intake/ intake/
COPY agent/ agent/
# Both extras: the same image runs the coordinator (default CMD) and the
# NAS-local INTAKE_COPY worker (data-intake-agent). The agent extra is Linux-
# safe (httpx/psutil/Pillow) — pywinauto lives in the separate `automation`
# extra and is never installed here.
COPY processors/ processors/
RUN pip install --no-cache-dir ".[coordinator,agent]"

COPY --from=webui /build/dist web/dist

# All state lives on the mounted volume: SQLite DB (+WAL), and an optional
# coordinator.yaml for advanced settings (templates, intake_defaults, …).
ENV DATA_INTAKE_WEBUI_DIR=/app/web/dist \
    DATA_INTAKE_DB_PATH=/data/coordinator.db \
    DATA_INTAKE_COORDINATOR_CONFIG=/data/coordinator.yaml \
    DATA_INTAKE_HOST=0.0.0.0 \
    DATA_INTAKE_PORT=8443
VOLUME /data
EXPOSE 8443

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8443/health', timeout=3)"

CMD ["data-intake-coordinator"]
