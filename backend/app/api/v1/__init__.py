from fastapi import APIRouter

from app.api.v1 import chat, comparisons, documents, events, metrics

api_router = APIRouter()
api_router.include_router(documents.router)
api_router.include_router(chat.router)
api_router.include_router(comparisons.router)
api_router.include_router(metrics.router)
api_router.include_router(events.router)
