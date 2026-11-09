from fastapi import APIRouter

from app.api.v1 import api_keys, chat, comparisons, config, documents, events, metrics
from app.core.errors import error_responses

# Every /api/v1 endpoint needs a key, validates its input and is rate limited. 404 is added to routes
# with an id in the path (app.main, _with_not_found_responses); route-specific errors are on the route.
COMMON_ERRORS = error_responses(401, 422, 429, 500)

api_router = APIRouter()
api_router.include_router(documents.router, responses=COMMON_ERRORS)
api_router.include_router(chat.router, responses=COMMON_ERRORS)
api_router.include_router(comparisons.router, responses=COMMON_ERRORS)
api_router.include_router(metrics.router, responses=COMMON_ERRORS)
api_router.include_router(events.router, responses=COMMON_ERRORS)
api_router.include_router(api_keys.router, responses=COMMON_ERRORS)
api_router.include_router(config.router, responses=COMMON_ERRORS)
