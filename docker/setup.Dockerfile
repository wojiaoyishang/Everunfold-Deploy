ARG EVERUNFOLD_PYTHON_IMAGE=python:3.12-slim-bookworm
ARG EVERUNFOLD_UV_IMAGE=ghcr.io/astral-sh/uv:0.9.0

FROM ${EVERUNFOLD_UV_IMAGE} AS uv
FROM ${EVERUNFOLD_PYTHON_IMAGE}
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PROJECT_ENVIRONMENT=/opt/setup/venv UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    PATH=/opt/setup/venv/bin:$PATH PYTHONDONTWRITEBYTECODE=1
WORKDIR /opt/setup
COPY pyproject.toml uv.lock ./
ARG EVERUNFOLD_PYPI_INDEX
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=docker/uv-sync.sh,target=/tmp/uv-sync.sh \
    sh /tmp/uv-sync.sh
COPY deploy/config_probes.py deploy/config_model.py deploy/config_validation.py deploy/probe.py deploy/storage.py ./
ENTRYPOINT ["python", "probe.py"]
