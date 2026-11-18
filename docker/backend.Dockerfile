# syntax=docker/dockerfile:1.7
# Build context: repository root.  docker build -f docker/backend.Dockerfile -t dayline-backend .
ARG PYTHON_VERSION=3.12

# --- build: install dependencies (cached on pyproject.toml), then the app --------
FROM python:${PYTHON_VERSION}-slim AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
WORKDIR /src/backend
COPY backend/pyproject.toml ./
# A stub package lets pip resolve and install the dependencies before the source is copied.
RUN mkdir -p app && echo '__version__ = "0"' > app/__init__.py \
 && pip install . && pip uninstall -y dayline-backend
COPY backend/app ./app
RUN pip install --no-deps .

# --- runtime ------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS runtime
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DAYLINE_HOME=/app
RUN groupadd --system --gid 10001 dayline \
 && useradd --system --uid 10001 --gid dayline --home-dir /app --no-create-home dayline
WORKDIR /app
COPY --from=build /opt/venv /opt/venv
# Code and config stay root-owned and read-only for the service user.
COPY config ./config
COPY skills/builtin ./skills/builtin
RUN mkdir -p skills/team skills/user
USER dayline
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)" || exit 1
CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--no-server-header"]

# --- dev: dev dependencies; source is bind-mounted at /app/backend ----------------
FROM runtime AS dev
USER root
COPY backend/pyproject.toml /tmp/pyproject/pyproject.toml
RUN mkdir -p /tmp/pyproject/app && echo '__version__ = "0"' > /tmp/pyproject/app/__init__.py \
 && pip install --no-cache-dir "/tmp/pyproject[dev]" && pip uninstall -y dayline-backend \
 && rm -rf /tmp/pyproject
ENV PYTHONPATH=/app/backend
USER dayline
CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload", "--reload-dir", "/app/backend/app"]
