# AGENTS.md: working on this codebase (humans and AI coding agents)

## Commands
- `make up` / `make down` / `make logs`: run the stack (UI + API on :9700)
- `make test`: pytest in Docker (fake AI provider, no key needed). Run it after every change.
- `make lint`: ruff + mypy. Must pass.
- `make smoke`: end-to-end check with the real model (costs ~$0.03)
- `make eval`: RAG quality eval. Run it after changing prompts, retrieval or models, and compare with the last result.
- Hot reload without rebuilding: `make dev-api`, `make dev-worker`, `make dev-ui` (see README §1)

## Layering rules
- `app/api/**`: HTTP only (parse request → call a service → shape the response). No SQL, no AI calls.
- `app/services/**`: business logic. Never import FastAPI. Async for the API side, sync for worker-side code.
- `app/llm/**`: the only place that knows a vendor SDK. Services call `get_provider()`.
- `app/llm/prompts/<name>.py`: one prompt per file with a `VERSION` string. Bump `VERSION` when you change the wording (it's stored with usage rows and is part of the answer-cache key).
- `app/workers/tasks.py`: thin wrappers that call services.
- Every model call records usage: `record_usage` (sync) or `arecord_usage` (async), with `operation` and `prompt_version`.
- Errors: raise an `AppError` subclass from `core/errors.py` with a stable `code`. Never return ad-hoc error JSON.

## Common changes, and where they go
| Change | Touch |
|---|---|
| New file type | `services/parsing.py` (a parser returning `Page`s) + `allowed_extensions` in `core/config.py` + `CONTENT_TYPES`/`MAGIC_BYTES` in `services/documents.py` + a test |
| New pipeline step | `services/processing.py` (`recorder.stage(...)`) + `ProcessingStage` enum |
| New AI feature | prompt file + Pydantic output in `llm/schemas.py` + fake-provider branch in `llm/fake_provider.py` + service + route + test |
| New endpoint | `api/v1/<area>.py` + schema in `schemas/` + test in `tests/test_api.py` |
| DB change | edit `models/`, then `alembic revision --autogenerate -m "..."`, review the file, `make test` |
| Swap or add an LLM vendor | new class implementing `llm/base.py::LLMProvider`, selected in `llm/__init__.py` |
| Tune retrieval | `services/retrieval.py` and `retrieval_*` / `min_relevance_score` settings, then `make eval` |

## Conventions
- Python 3.12, full type hints, Pydantic v2, SQLAlchemy 2.0 typed models, line length 120 (ruff).
- Config only through `core/config.py` (env vars); never hard-code secrets or URLs.
- Never commit `.env`. Never log API keys or document contents.
- Tests use real Postgres + Redis and `LLM_PROVIDER=fake`. Celery runs eagerly in tests (`tests/conftest.py`).
- Frontend: React + TypeScript, no UI library; the API client is in `frontend/src/lib/api.ts`; types mirror `app/schemas`.
