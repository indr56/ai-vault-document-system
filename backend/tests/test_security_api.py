"""API keys, WebSocket tickets and per-user API metrics."""

from types import SimpleNamespace

import httpx
import pytest

from app.api.v1 import events as events_api
from app.core.errors import UnauthorizedError
from tests.conftest import API_KEY


# ------------------------------------------------------------------ API keys (#18, #19)
async def test_create_use_list_and_revoke_api_key(client: httpx.AsyncClient) -> None:
    created = await client.post("/api-keys", json={"name": "ci"})
    assert created.status_code == 201
    body = created.json()
    raw = body["key"]
    assert raw.startswith("vault_") and body["key_prefix"] == raw[:10]

    # The new key authenticates (and its last use is recorded).
    assert (await client.get("/documents", headers={"X-API-Key": raw})).status_code == 200
    listed = {k["id"]: k for k in (await client.get("/api-keys")).json()}
    assert "key" not in listed[body["id"]]  # the full key is never shown again
    assert listed[body["id"]]["last_used_at"] is not None

    # Revoked from another key: it stops working.
    assert (await client.delete(f"/api-keys/{body['id']}")).status_code == 204
    assert (await client.get("/documents", headers={"X-API-Key": raw})).status_code == 401


async def test_cannot_revoke_the_key_in_use(client: httpx.AsyncClient) -> None:
    keys = (await client.get("/api-keys")).json()
    demo = next(k for k in keys if k["key_prefix"] == API_KEY[:8] or API_KEY.startswith(k["key_prefix"]))
    response = await client.delete(f"/api-keys/{demo['id']}")
    assert response.status_code == 409 and response.json()["error"]["code"] == "key_in_use"


# ------------------------------------------------------------------ WebSocket tickets (#22)
def _socket(headers: dict[str, str] | None = None, query: dict[str, str] | None = None) -> SimpleNamespace:
    return SimpleNamespace(headers=headers or {}, query_params=query or {})


async def test_websocket_ticket_is_single_use(client: httpx.AsyncClient) -> None:
    ticket = (await client.post("/events/ticket")).json()["ticket"]
    user_id = await events_api._websocket_user_id(_socket(query={"ticket": ticket}))
    assert user_id
    with pytest.raises(UnauthorizedError):  # replaying the same URL fails
        await events_api._websocket_user_id(_socket(query={"ticket": ticket}))


async def test_websocket_rejects_api_key_in_url(client: httpx.AsyncClient) -> None:
    with pytest.raises(UnauthorizedError):  # was accepted: the key ended up in access logs
        await events_api._websocket_user_id(_socket(query={"api_key": API_KEY}))
    assert await events_api._websocket_user_id(_socket(headers={"x-api-key": API_KEY}))  # header still works


async def test_ticket_requires_authentication(client: httpx.AsyncClient) -> None:
    response = await client.post("/events/ticket", headers={"X-API-Key": "wrong"})
    assert response.status_code == 401


# ------------------------------------------------------------------ per-user API metrics (#38)
async def test_api_metrics_show_only_your_requests(client: httpx.AsyncClient) -> None:
    other_key = (await client.post("/api-keys", json={"name": "other"})).json()["key"]
    # Same user, different key: still the caller's own data. A different USER is needed to prove
    # isolation, so create one directly.
    from app.core.security import hash_api_key
    from app.db.session import get_async_sessionmaker
    from app.models import ApiKey, User

    async with get_async_sessionmaker()() as db:
        stranger = User(email="stranger@example.com")
        db.add(stranger)
        await db.flush()
        db.add(ApiKey(user_id=stranger.id, name="s", key_hash=hash_api_key("stranger-key"), key_prefix="stranger"))
        await db.commit()

    for _ in range(3):
        await client.get("/documents?limit=7", headers={"X-API-Key": other_key})
    stranger_view = (await client.get("/metrics/api", headers={"X-API-Key": "stranger-key"})).json()
    routes = {(r["method"], r["route"]) for r in stranger_view["routes"]}
    assert ("GET", "/api/v1/documents") not in routes  # was: every user's routes and counts
    mine = (await client.get("/metrics/api")).json()
    assert any(r["route"] == "/api/v1/documents" for r in mine["routes"])


# ------------------------------------------------------------------ WebSocket end to end
@pytest.fixture
def fresh_clients():  # type: ignore[no-untyped-def]
    """TestClient runs the app on its own event loop; async DB/Redis clients are per loop."""
    from app.db import session
    from app.services import cache

    def reset() -> None:
        cache.get_aredis.cache_clear()
        session.get_async_engine.cache_clear()
        session.get_async_sessionmaker.cache_clear()

    reset()
    yield
    reset()


@pytest.mark.usefixtures("fresh_clients")
def test_websocket_relays_events_with_ticket() -> None:
    from fastapi.testclient import TestClient
    from sqlalchemy import select
    from starlette.websockets import WebSocketDisconnect

    from app.core.security import hash_api_key
    from app.db.session import sync_session
    from app.main import app
    from app.models import ApiKey
    from app.services.events import publish

    headers = {"X-API-Key": API_KEY}
    with TestClient(app) as http:
        ticket = http.post("/api/v1/events/ticket", headers=headers).json()["ticket"]
        with sync_session() as db:
            owner = db.execute(select(ApiKey.user_id).where(ApiKey.key_hash == hash_api_key(API_KEY))).scalar_one()
        with http.websocket_connect(f"/api/v1/events?ticket={ticket}") as ws:
            assert ws.receive_json() == {"type": "connected"}
            publish(owner, "document.ready", {"document_id": "d1"})
            assert ws.receive_json() == {"type": "document.ready", "document_id": "d1"}
        with pytest.raises(WebSocketDisconnect) as refused, http.websocket_connect(f"/api/v1/events?ticket={ticket}"):
            pass
        assert refused.value.code == 1008  # the used ticket is refused


# ------------------------------------------------------------------ client config (#12, #20)
async def test_config_exposes_upload_rules(client: httpx.AsyncClient) -> None:
    from app.core.config import get_settings

    upload = (await client.get("/config")).json()["upload"]
    settings = get_settings()
    assert upload["allowed_extensions"] == list(settings.allowed_extensions)  # the UI's file picker uses this
    assert upload["max_upload_mb"] == settings.max_upload_mb
    assert upload["max_documents_per_user"] == 10_000  # was 1,000


async def test_batch_limit_comes_from_settings(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "max_batch_files", 1)
    files = [("files", ("a.txt", b"a" * 40)), ("files", ("b.txt", b"b" * 40))]
    response = await client.post("/documents/batch", files=files)
    assert response.status_code == 422 and response.json()["error"]["code"] == "too_many_files"
