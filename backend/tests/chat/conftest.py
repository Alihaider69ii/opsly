import uuid
from types import SimpleNamespace

import pytest

from app.api.public_chat.router import get_chat_agent
from app.main import app
from app.services.ai.agent import ChatAgent
from tests.chat.helpers import RecordingLLM, SpyEmbedder, StubSearch, make_hit

SHIPPING_A = make_hit(
    "Brand A ships within two business days. Shipping is free on orders over 50 dollars.",
    url="https://brand-a.example/shipping",
    title="Shipping Policy",
    score=0.9,
)
SECRET_B = make_hit(
    "Brand B's secret wholesale price list: 3 dollars per bar.",
    url="https://brand-b.example/wholesale",
    title="Wholesale",
    score=0.95,
)


@pytest.fixture()
def keys(client, tenants):
    """Public keys and organization ids of two separate businesses."""
    a, b = tenants
    return SimpleNamespace(
        a=client.get("/api/chat/widget", headers=a.headers).json()["public_key"],
        b=client.get("/api/chat/widget", headers=b.headers).json()["public_key"],
        a_org=uuid.UUID(a.org["id"]),
        b_org=uuid.UUID(b.org["id"]),
        a_headers=a.headers,
        b_headers=b.headers,
    )


@pytest.fixture()
def llm():
    return RecordingLLM()


@pytest.fixture()
def search(keys):
    return StubSearch({keys.a_org: [SHIPPING_A], keys.b_org: [SECRET_B]})


@pytest.fixture()
def chat(client, llm, search, app_settings):
    """Install a test agent: scripted LLM, spy embedder, per-organization stub search."""
    agent = ChatAgent(llm=llm, embedder=SpyEmbedder(), settings=app_settings, search=search)
    app.dependency_overrides[get_chat_agent] = lambda: agent
    return agent


def open_chat(client, key, *, visitor_id=None, headers=None):
    body = {"visitor_id": visitor_id} if visitor_id else {}
    response = client.post(
        f"/api/public/chat/{key}/conversations", json=body, headers=headers or {}
    )
    assert response.status_code == 201, response.text
    data = response.json()
    return data["conversation_id"], data["token"]


def say(client, key, conversation_id, token, text, *, headers=None):
    return client.post(
        f"/api/public/chat/{key}/messages",
        json={"conversation_id": conversation_id, "content": text},
        headers={"X-Conversation-Token": token, **(headers or {})},
    )
