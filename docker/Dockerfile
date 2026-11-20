ARG EVERUNFOLD_PYTHON_IMAGE=python:3.12-slim-bookworm
ARG EVERUNFOLD_NODE_IMAGE=node:22.15.1-bookworm-slim
ARG EVERUNFOLD_UV_IMAGE=ghcr.io/astral-sh/uv:0.9.0

FROM ${EVERUNFOLD_NODE_IMAGE} AS node
FROM ${EVERUNFOLD_UV_IMAGE} AS uv
FROM ${EVERUNFOLD_PYTHON_IMAGE}
LABEL org.opencontainers.image.title="Everunfold" \
      org.opencontainers.image.description="Everunfold frontend, API, Celery worker and scheduler" \
      org.opencontainers.image.source="https://github.com/wojiaoyishang/Everunfold-Deploy" \
      org.everunfold.build.npm-mirror="1"
ARG BACKEND_REVISION
ARG FRONTEND_REVISION
LABEL org.everunfold.backend.revision=$BACKEND_REVISION \
      org.everunfold.frontend.revision=$FRONTEND_REVISION
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
COPY --from=uv /uv /usr/local/bin/uv
ARG EVERUNFOLD_APT_MIRROR
ARG EVERUNFOLD_APT_SECURITY_MIRROR
RUN --mount=type=bind,source=docker/apt-mirrors.sh,target=/tmp/apt-mirrors.sh \
    sh /tmp/apt-mirrors.sh && apt-get -o APT::Update::Error-Mode=any update \
    && apt-get install -y --no-install-recommends ca-certificates git rsync libstdc++6 \
    && rm -rf /var/lib/apt/lists/* \
    && ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx
ENV UV_PROJECT_ENVIRONMENT=/opt/backend/venv UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    PATH=/opt/backend/venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    NEXT_TELEMETRY_DISABLED=1 EVERUNFOLD_RUNTIME=/config/runtime.yaml
WORKDIR /opt/backend
COPY backend/pyproject.toml backend/uv.lock ./
ARG EVERUNFOLD_PYPI_INDEX
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=docker/uv-sync.sh,target=/tmp/uv-sync.sh \
    sh /tmp/uv-sync.sh
COPY deploy /opt/everunfold/deploy
COPY --chmod=755 frontend-entrypoint.sh /usr/local/bin/frontend-build
COPY docker/npm-ci.sh /usr/local/bin/npm-ci.sh
RUN mkdir -p /build/work /build/npm-cache /workspace /output /plugins /data /config \
    && chmod 1777 /build /build/work /build/npm-cache /output /plugins /data /config
WORKDIR /workspace
USER 1000:1000
EXPOSE 8001 3000
HEALTHCHECK --interval=30s --timeout=12s --start-period=120s --retries=3 \
    CMD ["python", "/opt/everunfold/deploy/runtime.py", "health"]
ENTRYPOINT ["python", "/opt/everunfold/deploy/runtime.py"]
CMD ["serve"]
