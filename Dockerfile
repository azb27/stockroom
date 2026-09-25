# The public demo image (Hugging Face Spaces, Docker SDK). See ADR 0007.
# It holds the app, the cleaned warehouse and the forecasts. It never holds ground_truth.duckdb (the
# eval's answer key) or the dirt manifest: tools can't read what isn't there.

ARG BASE=docker.io/library  # e.g. mirror.gcr.io/library when Docker Hub rate-limits

FROM ${BASE}/node:22-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN --mount=type=secret,id=proxy_ca,required=false \
    if [ -f /run/secrets/proxy_ca ]; then export NODE_EXTRA_CA_CERTS=/run/secrets/proxy_ca; fi; \
    npm ci --no-audit --no-fund
COPY web/ ./
RUN NEXT_TELEMETRY_DISABLED=1 npm run build

FROM ${BASE}/python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -m -u 1000 user
USER user
ENV HOME=/home/user PATH=/home/user/.local/bin:$PATH PYTHONUNBUFFERED=1
WORKDIR /app
COPY --chown=user pyproject.toml ./
COPY --chown=user src ./src
# The optional build secret is only for building behind a TLS-intercepting proxy; it is never stored in the image.
RUN --mount=type=secret,id=proxy_ca,required=false,uid=1000 \
    if [ -f /run/secrets/proxy_ca ]; then export PIP_CERT=/run/secrets/proxy_ca; fi; \
    pip install --no-cache-dir --user -e .
COPY --chown=user docs/results/eval_summary.json docs/results/eval_summary.json
COPY --chown=user data/warehouse.duckdb data/forecast.duckdb data/
COPY --from=web --chown=user /web/out web/out
RUN test ! -e data/ground_truth.duckdb && test ! -e data/dirt_manifest.json && mkdir -p runs
ENV STOCKROOM_APP_DB=/tmp/app.duckdb STOCKROOM_TRUST_PROXY=1 HOST=0.0.0.0 PORT=7860
EXPOSE 7860
HEALTHCHECK CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:7860/api/health')"
CMD ["stockroom-web"]
