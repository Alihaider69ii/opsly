"""Phase 3 on real infrastructure: pgvector retrieval inside the agent, conversations in Postgres,
the whole crawl -> index -> chat flow, and the Redis-backed rate limiter."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import redis as redis_lib
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.api.public_chat.router import get_chat_agent
from app.core.config import get_settings
from app.core.rate_limit import RateLimiter
from app.db.models import (
    Conversation,
    Document,
    DocumentStatus,
    KnowledgeChunk,
    KnowledgeSource,
    KnowledgeSourceType,
    Message,
    MessageRole,
    Organization,
)
from app.main import app
from app.services.ai.agent import ChatAgent
from app.services.ai.embeddings import embedding_input
from app.services.ai.providers.fake import FakeEmbeddingProvider
from tests.chat.conftest import open_chat, say
from tests.chat.helpers import RecordingLLM
from tests.crawler.sites import serve
from tests.integration.conftest import TEST_REDIS_URL
from tests.integration.test_queue_and_pipeline import SOURCES, site_routes, worker_tick

EMBEDDER = FakeEmbeddingProvider()
FALLBACK_START = "I don't have enough information"


@pytest.fixture()
def llm():
    return RecordingLLM()


@pytest.fixture()
def real_agent(pg_client, llm, integration_settings):
    """The production agent wiring (real pgvector `search_knowledge`), with only the LLM scripted."""
    agent = ChatAgent(llm=llm, embedder=EMBEDDER, settings=integration_settings)
    app.dependency_overrides[get_chat_agent] = lambda: agent
    return agent


@pytest.fixture()
def tenants_with_keys(pg_client, two_tenants):
    (ha, oa), (hb, ob) = two_tenants
    return {
        "a": {
            "headers": ha,
            "org": oa,
            "key": pg_client.get("/api/chat/widget", headers=ha).json()["public_key"],
        },
        "b": {
            "headers": hb,
            "org": ob,
            "key": pg_client.get("/api/chat/widget", headers=hb).json()["public_key"],
        },
    }


def add_knowledge(pg_factory, organization_id, pages: dict[str, tuple[str, str]]) -> None:
    """Index pages directly: url -> (title, text). One chunk per page, embedded for real."""
    with pg_factory() as db:
        source = KnowledgeSource(
            organization_id=uuid.UUID(organization_id),
            type=KnowledgeSourceType.WEBSITE,
            name="site",
            url=f"https://{uuid.uuid4().hex[:8]}.example/",
        )
        db.add(source)
        db.flush()
        for url, (title, body) in pages.items():
            doc = Document(
                organization_id=source.organization_id,
                knowledge_source_id=source.id,
                url=url,
                title=title,
                content=body,
                content_hash=uuid.uuid4().hex * 2,
                status=DocumentStatus.READY,
            )
            db.add(doc)
            db.flush()
            db.add(
                KnowledgeChunk(
                    organization_id=source.organization_id,
                    document_id=doc.id,
                    chunk_index=0,
                    content=body,
                    character_count=len(body),
                    embedding=EMBEDDER.embed([embedding_input(title, body)])[0],
                    embedding_model=EMBEDDER.model_id,
                )
            )
        db.commit()


A_PAGES = {
    "https://brand-a.example/shipping": (
        "Shipping Policy",
        "Brand A orders ship within two business days. Standard delivery takes five to seven days. Orders over fifty dollars ship free.",
    ),
    "https://brand-a.example/returns": (
        "Returns",
        "Unopened items can be returned within thirty days for a full refund.",
    ),
}
B_PAGES = {
    "https://brand-b.example/wholesale": (
        "Wholesale",
        "Brand B secret wholesale price list: three dollars per bar, minimum order one hundred bars.",
    ),
}


# --- real retrieval inside the agent ---------------------------------------------------------------------------


def test_the_agent_retrieves_the_right_chunk_from_pgvector_and_answers_from_it(
    pg_client, tenants_with_keys, pg_factory, real_agent, llm
):
    a = tenants_with_keys["a"]
    add_knowledge(pg_factory, a["org"]["id"], A_PAGES)
    conversation_id, token = open_chat(pg_client, a["key"])
    r = say(
        pg_client,
        a["key"],
        conversation_id,
        token,
        "how long does standard delivery take and when do orders ship",
    )
    assert r.status_code == 200
    assert (
        "five to seven days" in r.json()["message"]["content"]
    )  # the fake LLM quotes its top source
    knowledge = llm.calls[0][-2].content
    assert "SOURCE 1\nTitle: Shipping Policy\nURL: https://brand-a.example/shipping" in knowledge
    assert "thirty days" not in knowledge.split("SOURCE 2")[0]  # the returns page ranked below


def test_questions_the_knowledge_base_cannot_answer_get_the_fallback_without_an_llm_call(
    pg_client, tenants_with_keys, pg_factory, real_agent, llm
):
    a = tenants_with_keys["a"]
    add_knowledge(pg_factory, a["org"]["id"], A_PAGES)
    conversation_id, token = open_chat(pg_client, a["key"])
    r = say(pg_client, a["key"], conversation_id, token, "motorcycles tires engine horsepower")
    assert r.status_code == 200 and r.json()["message"]["content"].startswith(FALLBACK_START)
    assert llm.calls == []


def test_an_organization_without_knowledge_gets_the_fallback_not_someone_elses_answer(
    pg_client, tenants_with_keys, pg_factory, real_agent, llm
):
    add_knowledge(pg_factory, tenants_with_keys["a"]["org"]["id"], A_PAGES)
    b = tenants_with_keys["b"]
    conversation_id, token = open_chat(pg_client, b["key"])
    r = say(
        pg_client,
        b["key"],
        conversation_id,
        token,
        "how long does standard delivery take and when do orders ship",
    )
    assert r.json()["message"]["content"].startswith(FALLBACK_START) and llm.calls == []


def test_two_organizations_ask_the_same_question_and_only_see_their_own_knowledge(
    pg_client, tenants_with_keys, pg_factory, real_agent, llm
):
    a, b = tenants_with_keys["a"], tenants_with_keys["b"]
    add_knowledge(pg_factory, a["org"]["id"], A_PAGES)
    add_knowledge(pg_factory, b["org"]["id"], B_PAGES)
    question = "what is your wholesale price list per bar"
    a_id, a_token = open_chat(pg_client, a["key"])
    b_id, b_token = open_chat(pg_client, b["key"])

    # B's knowledge is the perfect match for this question; A must not be able to reach it
    say(pg_client, a["key"], a_id, a_token, question)
    sent_to_llm_for_a = "\n".join(m.content for call in llm.calls for m in call)
    assert "three dollars per bar" not in sent_to_llm_for_a

    llm.calls.clear()
    r = say(pg_client, b["key"], b_id, b_token, question)
    assert "three dollars per bar" in r.json()["message"]["content"]
    assert "Brand A orders ship" not in "\n".join(m.content for m in llm.calls[0])


def test_follow_ups_use_the_previous_turn_for_retrieval_on_real_vectors(
    pg_client, tenants_with_keys, pg_factory, real_agent, llm
):
    a = tenants_with_keys["a"]
    add_knowledge(pg_factory, a["org"]["id"], A_PAGES)
    conversation_id, token = open_chat(pg_client, a["key"])
    say(
        pg_client,
        a["key"],
        conversation_id,
        token,
        "how long does standard delivery take and when do orders ship",
    )
    r = say(
        pg_client, a["key"], conversation_id, token, "is it free?"
    )  # alone, this matches nothing
    assert r.status_code == 200
    assert (
        len(llm.calls) == 2 and "ship free" in llm.calls[1][-2].content
    )  # found via the prior turn


# --- conversations in Postgres -------------------------------------------------------------------------------------


def test_conversations_persist_in_postgres_with_ordered_messages(
    pg_client, tenants_with_keys, pg_factory, real_agent
):
    a = tenants_with_keys["a"]
    add_knowledge(pg_factory, a["org"]["id"], A_PAGES)
    conversation_id, token = open_chat(pg_client, a["key"], visitor_id="visitor-pg-12345")
    for q in (
        "how long does standard delivery take and when do orders ship",
        "orders over fifty dollars ship free standard delivery",
    ):
        assert say(pg_client, a["key"], conversation_id, token, q).status_code == 200
    with pg_factory() as db:
        conversation = db.get(Conversation, uuid.UUID(conversation_id))
        rows = list(
            db.scalars(
                select(Message)
                .where(Message.conversation_id == conversation.id)
                .order_by(Message.position)
            )
        )
        assert conversation.message_count == 4 and conversation.locked_until is None
        assert [m.position for m in rows] == [1, 2, 3, 4]
        assert [m.role for m in rows] == [MessageRole.USER, MessageRole.ASSISTANT] * 2
        assert {str(m.organization_id) for m in rows} == {a["org"]["id"]}
        assert (
            rows[1].meta["grounded"] is True
            and rows[1].meta["sources"][0]["title"] == "Shipping Policy"
        )
    history = pg_client.get(
        f"/api/public/chat/{a['key']}/conversations/{conversation_id}",
        headers={"X-Conversation-Token": token},
    ).json()
    assert [m["role"] for m in history["messages"]] == ["user", "assistant", "user", "assistant"]


def test_postgres_rejects_a_message_attached_to_another_organizations_conversation(
    pg_client, tenants_with_keys, pg_factory
):
    a, b = tenants_with_keys["a"], tenants_with_keys["b"]
    conversation_id, _ = open_chat(pg_client, a["key"])
    with pg_factory() as db, pytest.raises(IntegrityError):
        db.add(
            Message(
                organization_id=uuid.UUID(b["org"]["id"]),  # wrong tenant for this conversation
                conversation_id=uuid.UUID(conversation_id),
                position=1,
                role=MessageRole.USER,
                content="smuggled",
            )
        )
        db.commit()


def test_deleting_an_organization_removes_its_conversations_and_messages(
    pg_client, tenants_with_keys, pg_factory, real_agent
):
    a = tenants_with_keys["a"]
    add_knowledge(pg_factory, a["org"]["id"], A_PAGES)
    conversation_id, token = open_chat(pg_client, a["key"])
    say(pg_client, a["key"], conversation_id, token, "how long does standard delivery take")
    with pg_factory() as db:
        db.delete(db.get(Organization, uuid.UUID(a["org"]["id"])))
        db.commit()
        for table in ("conversations", "messages", "chat_widgets"):
            assert (
                db.scalar(
                    text(f"SELECT count(*) FROM {table} WHERE organization_id = :o"),
                    {"o": uuid.UUID(a["org"]["id"])},
                )
                == 0
            )
    assert (
        pg_client.get(f"/api/public/chat/{a['key']}/config").status_code == 404
    )  # the key died with it


def test_the_one_reply_at_a_time_lock_works_with_timestamptz(
    pg_client, tenants_with_keys, pg_factory, real_agent
):
    a = tenants_with_keys["a"]
    add_knowledge(pg_factory, a["org"]["id"], A_PAGES)
    conversation_id, token = open_chat(pg_client, a["key"])
    with pg_factory() as db:
        db.get(Conversation, uuid.UUID(conversation_id)).locked_until = datetime.now(
            UTC
        ) + timedelta(seconds=60)
        db.commit()
    assert say(pg_client, a["key"], conversation_id, token, "shipping").status_code == 409
    with pg_factory() as db:
        db.get(Conversation, uuid.UUID(conversation_id)).locked_until = datetime.now(
            UTC
        ) - timedelta(seconds=1)
        db.commit()
    assert (
        say(
            pg_client, a["key"], conversation_id, token, "how long does standard delivery take"
        ).status_code
        == 200
    )


# --- the whole product flow: crawl a website, then chat about it -----------------------------------------------------


def test_crawl_a_website_then_a_visitor_chats_about_it(
    pg_client, two_tenants, pg_factory, redis_queue, integration_settings, llm
):
    (ha, oa), (hb, ob) = two_tenants
    agent = ChatAgent(llm=llm, embedder=EMBEDDER, settings=integration_settings)
    app.dependency_overrides[get_chat_agent] = lambda: agent
    key_a = pg_client.get("/api/chat/widget", headers=ha).json()["public_key"]
    key_b = pg_client.get("/api/chat/widget", headers=hb).json()["public_key"]

    with serve(site_routes()) as port:
        integration_settings.crawl_allowed_ports = f"80,443,{port}"
        created = pg_client.post(SOURCES, json={"url": f"http://site.test:{port}"}, headers=ha)
        assert created.status_code == 201
        assert worker_tick(pg_factory, redis_queue, integration_settings)[1] == "completed"

    conversation_id, token = open_chat(pg_client, key_a)
    reply = say(
        pg_client,
        key_a,
        conversation_id,
        token,
        "how long does standard delivery take and when do orders ship",
    )
    assert reply.status_code == 200
    assert (
        "five to seven days" in reply.json()["message"]["content"]
    )  # an answer built from the crawled page
    assert "SOURCE 1\nTitle: Shipping Policy" in llm.calls[0][-2].content

    follow_up = say(pg_client, key_a, conversation_id, token, "and is shipping free?")
    assert follow_up.status_code == 200 and "free" in llm.calls[1][-2].content

    # A self-contained question about something the site never mentions: nothing relevant is
    # retrieved, so the fallback is returned and the model is not consulted.
    calls_before = len(llm.calls)
    off_topic = say(
        pg_client,
        key_a,
        conversation_id,
        token,
        "do you sell motorcycles tires engine parts horsepower upgrades for sports bikes",
    )
    assert off_topic.json()["message"]["content"].startswith(FALLBACK_START)
    assert len(llm.calls) == calls_before

    # the other business, with nothing crawled, learns none of it
    b_id, b_token = open_chat(pg_client, key_b)
    nothing = say(
        pg_client,
        key_b,
        b_id,
        b_token,
        "how long does standard delivery take and when do orders ship",
    )
    assert nothing.json()["message"]["content"].startswith(FALLBACK_START)
    assert (
        pg_client.get(
            f"/api/public/chat/{key_b}/conversations/{conversation_id}",
            headers={"X-Conversation-Token": token},
        ).status_code
        == 404
    )


# --- shared rate limiting through Redis ---------------------------------------------------------------------------------------


@pytest.fixture()
def redis_limiter():
    settings = get_settings()
    original = (settings.rate_limit_backend, settings.redis_url)
    settings.rate_limit_backend, settings.redis_url = "redis", TEST_REDIS_URL
    limiter_a, limiter_b = RateLimiter(), RateLimiter()
    client = redis_lib.Redis.from_url(TEST_REDIS_URL)
    for key in client.scan_iter("bizflow:ratelimit:itest:*"):
        client.delete(key)
    yield limiter_a, limiter_b
    for key in client.scan_iter("bizflow:ratelimit:itest:*"):
        client.delete(key)
    settings.rate_limit_backend, settings.redis_url = original


def test_two_api_workers_share_one_rate_limit_through_redis(redis_limiter):
    worker_one, worker_two = redis_limiter
    key = f"itest:{uuid.uuid4().hex}"
    results = [
        worker_one.allow(key, 3, 60),
        worker_two.allow(key, 3, 60),
        worker_one.allow(key, 3, 60),
        worker_two.allow(key, 3, 60),
    ]
    assert results == [True, True, True, False]  # the 4th request, on the OTHER worker, is refused


def test_redis_window_expires(redis_limiter):
    worker, _ = redis_limiter
    key = f"itest:{uuid.uuid4().hex}"
    assert worker.allow(key, 1, 1) and not worker.allow(key, 1, 1)
    import time

    time.sleep(1.2)
    assert worker.allow(key, 1, 1)


def test_a_redis_outage_degrades_to_in_process_limits_instead_of_failing(redis_limiter):
    settings = get_settings()
    settings.redis_url = "redis://127.0.0.1:1/0"  # nothing listens here
    worker = RateLimiter()
    key = f"itest:{uuid.uuid4().hex}"
    assert [worker.allow(key, 2, 60) for _ in range(3)] == [True, True, False]  # still limiting


def test_a_short_message_after_an_answer_is_retrieved_together_with_the_previous_turn(
    pg_client, tenants_with_keys, pg_factory, real_agent, llm
):
    """Documented trade-off: only the model can tell a follow-up ("is it free?") from a topic
    change, so short messages are searched with the previous turn and the model is then instructed
    (by the system prompt) to decline what the information does not cover."""
    a = tenants_with_keys["a"]
    add_knowledge(pg_factory, a["org"]["id"], A_PAGES)
    conversation_id, token = open_chat(pg_client, a["key"])
    say(
        pg_client,
        a["key"],
        conversation_id,
        token,
        "how long does standard delivery take and when do orders ship",
    )
    say(pg_client, a["key"], conversation_id, token, "motorcycles tires engine horsepower")
    assert len(llm.calls) == 2  # the model was consulted, with the grounding rules in its prompt
    assert "Never invent or guess" in llm.calls[1][0].content
