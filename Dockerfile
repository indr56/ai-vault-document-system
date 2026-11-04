# syntax=docker/dockerfile:1

# ---- 1. Build the React UI -------------------------------------------------------------------
FROM node:22-alpine AS ui
WORKDIR /ui
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# ---- 2. Python runtime (API and worker share this image) --------------------------------------
FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TIKTOKEN_CACHE_DIR=/opt/tiktoken
WORKDIR /app

COPY backend/requirements.txt ./
RUN pip install -r requirements.txt \
    # Pre-download the tokenizer so containers never need network access to start processing.
    && python -c "import tiktoken; tiktoken.get_encoding('cl100k_base')"

COPY backend/ ./
COPY --from=ui /ui/dist ./static
COPY samples /samples

RUN useradd --create-home --uid 1000 vault \
    && mkdir -p /data/uploads && chown -R vault:vault /data /app
USER vault

ENTRYPOINT ["./scripts/entrypoint.sh"]

# ---- 3. Test image (`make test`): adds dev tools and the sample documents ---------------------
FROM base AS test
USER root
RUN pip install -r requirements-dev.txt
USER vault
CMD ["pytest", "-q"]

# ---- 4. Final runtime image (default target) --------------------------------------------------
FROM base AS app
EXPOSE 8000
CMD ["api"]
