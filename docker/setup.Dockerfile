FROM ghcr.io/astral-sh/uv:0.9.0 AS uv
FROM python:3.12-slim-bookworm
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PROJECT_ENVIRONMENT=/opt/setup/venv UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    PATH=/opt/setup/venv/bin:$PATH PYTHONDONTWRITEBYTECODE=1
WORKDIR /opt/setup
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev
COPY deploy/config_probes.py deploy/config_model.py deploy/config_validation.py deploy/probe.py deploy/storage.py ./
ENTRYPOINT ["python", "probe.py"]
