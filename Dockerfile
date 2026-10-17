FROM python:3.12-slim AS builder
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /build
COPY pyproject.toml ./
RUN python -m venv /opt/venv \
    && python -c 'import tomllib; print("\n".join(tomllib.load(open("pyproject.toml", "rb"))["project"]["dependencies"]))' > /tmp/requirements.txt \
    && GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=http.version GIT_CONFIG_VALUE_0=HTTP/1.1 \
       /opt/venv/bin/pip install --no-cache-dir -r /tmp/requirements.txt
# Materialize the pinned runtime's tokenizer assets during the networked build.
# Cold runtime startup must not wait for a vocabulary download before HTTP can listen.
RUN TIKTOKEN_CACHE_DIR=/opt/tiktoken-cache /opt/venv/bin/python -c \
    'from lumen.ai.vector_store import NumpyVectorStore; NumpyVectorStore()' \
    && chmod -R a+rX /opt/tiktoken-cache
COPY README.md LICENSE THIRD_PARTY_NOTICES.md ./
COPY src/ src/
RUN /opt/venv/bin/pip install --no-cache-dir --no-deps .

FROM python:3.12-slim AS runtime
ARG OPSLENS_BUILD_REVISION=UNKNOWN
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    TIKTOKEN_CACHE_DIR=/opt/tiktoken-cache \
    OPSLENS_UI_HOST=0.0.0.0 OPSLENS_UI_PORT=5006 \
    OPSLENS_BUILD_REVISION=$OPSLENS_BUILD_REVISION
RUN groupadd --gid 10001 opslens && useradd --uid 10001 --gid opslens --create-home opslens
WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /opt/tiktoken-cache /opt/tiktoken-cache
COPY LICENSE THIRD_PARTY_NOTICES.md ./
COPY --chown=opslens:opslens data/metrics/ data/metrics/
COPY --chown=opslens:opslens data/knowledge/ data/knowledge/
COPY --chown=opslens:opslens data/sql_policy/ data/sql_policy/
COPY --chown=opslens:opslens data/validation/ data/validation/
COPY --chown=opslens:opslens data/product/ data/product/
COPY --chown=opslens:opslens alembic/ alembic/
COPY --chown=opslens:opslens alembic.ini ./
COPY --chown=opslens:opslens scripts/serve_ui.py scripts/readiness_check.py scripts/summarize_product_events.py scripts/
RUN mkdir -p data/demo/generated && chown -R opslens:opslens data/demo
USER 10001:10001
EXPOSE 5006
HEALTHCHECK --interval=30s --timeout=20s --start-period=120s --retries=5 \
    CMD ["python", "scripts/readiness_check.py"]
CMD ["python", "scripts/serve_ui.py"]
