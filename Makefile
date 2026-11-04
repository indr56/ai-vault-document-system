# Common tasks. Everything runs in Docker; only `make dev-*` needs local Python/Node.
.PHONY: help setup up down logs ps test smoke samples lint eval reset dev-api dev-worker dev-ui

help:            ## Show this help
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  \033[1m%-12s\033[0m %s\n", $$1, $$2}'

setup:           ## Create .env from the template (then add OPENAI_API_KEY)
	@test -f .env || (cp .env.example .env && echo "Created .env — add your OPENAI_API_KEY")

up: setup        ## Build and start everything -> http://localhost:9700
	docker compose up --build -d
	@echo "Waiting for the API..." && until curl -sf http://localhost:$${API_PORT:-9700}/health >/dev/null; do sleep 2; done
	@echo "\n  UI:       http://localhost:$${API_PORT:-9700}\n  API docs: http://localhost:$${API_PORT:-9700}/docs\n  API key:  vault-demo-key\n"

down:            ## Stop containers (data is kept)
	docker compose down

reset:           ## Stop containers and delete all data
	docker compose down -v

logs:            ## Follow API and worker logs
	docker compose logs -f api worker

ps:              ## Show container status
	docker compose ps

test:            ## Run the test suite in Docker (fake AI, no API key needed)
	docker compose --profile test run --rm --build tests

smoke:           ## End-to-end check against the running stack with the real AI provider
	docker compose exec api python scripts/smoke_test.py --base-url http://localhost:8000

eval:            ## Run the RAG quality evaluation against the running stack
	docker compose exec api python evals/run_eval.py --base-url http://localhost:8000

lint:            ## Lint + type-check (in the test image)
	docker compose --profile test run --rm --build tests sh -c "ruff check . && ruff format --check . && mypy app"

samples:         ## Regenerate the sample documents in ./samples
	docker compose --profile test run --rm --build -v $(PWD)/samples:/samples tests python scripts/generate_samples.py

# ---- local development without Docker for the app (db + redis still in Docker) ----
dev-api:         ## Run the API locally with auto-reload (needs backend venv)
	docker compose up -d db redis
	cd backend && set -a && { [ -f ../.env ] && . ../.env || true; } && set +a && DATABASE_URL=postgresql+asyncpg://vault:vault@localhost:9701/vault SYNC_DATABASE_URL=postgresql+psycopg://vault:vault@localhost:9701/vault REDIS_URL=redis://localhost:9702/0 CELERY_BROKER_URL=redis://localhost:9702/1 CELERY_RESULT_BACKEND=redis://localhost:9702/2 STORAGE_DIR=../data/uploads sh -c "alembic upgrade head && uvicorn app.main:app --reload --port 9700"

dev-worker:      ## Run a Celery worker locally (needs backend venv)
	cd backend && set -a && { [ -f ../.env ] && . ../.env || true; } && set +a && SYNC_DATABASE_URL=postgresql+psycopg://vault:vault@localhost:9701/vault REDIS_URL=redis://localhost:9702/0 CELERY_BROKER_URL=redis://localhost:9702/1 CELERY_RESULT_BACKEND=redis://localhost:9702/2 STORAGE_DIR=../data/uploads celery -A app.workers.celery_app worker -B -Q documents,analysis,celery --loglevel INFO

dev-ui:          ## Run the UI dev server on :5173 (proxies to the API on :9700)
	cd frontend && npm install && npm run dev
