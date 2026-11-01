import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.db.models import (
    Conversation,
    ConversationChannel,
    ConversationStatus,
    Message,
    MessageRole,
)
from app.services.ai.provider import LLMError
from tests.chat.conftest import open_chat, say

UNAVAILABLE = "The assistant is temporarily unavailable. Please try again in a moment."


def messages_in_db(db):
    db.expire_all()
    return list(db.scalars(select(Message).order_by(Message.conversation_id, Message.position)))


# --- the basic flow ----------------------------------------------------------------------------------------------


def test_widget_config_exposes_only_the_name_and_greeting(client, tenants, keys, chat):
    r = client.get(f"/api/public/chat/{keys.a}/config")
    assert r.status_code == 200
    assert r.json() == {
        "business_name": "Brand A",
        "welcome_message": "Hi! How can I help you today?",
    }
    assert keys.a not in r.text.replace(keys.a, "", 1)  # the key is not echoed back
    assert str(keys.a_org) not in r.text


def test_conversation_is_created_for_the_keys_organization_on_the_website_channel(
    client, keys, chat, db
):
    conversation_id, token = open_chat(client, keys.a, visitor_id="visitor-abc12345")
    row = db.get(Conversation, uuid.UUID(conversation_id))
    assert row.organization_id == keys.a_org
    assert row.channel == ConversationChannel.WEBSITE and row.status == ConversationStatus.OPEN
    assert (
        row.visitor_id == "visitor-abc12345" and row.customer_id is None and row.message_count == 0
    )
    assert len(token) >= 40 and token not in row.token_hash  # only a hash is stored
    assert row.token_hash != token


def test_asking_a_question_returns_a_grounded_answer_and_stores_both_messages(
    client, keys, chat, llm, db
):
    llm.replies = ["Brand A ships within two business days."]
    conversation_id, token = open_chat(client, keys.a)
    r = say(client, keys.a, conversation_id, token, "How long does shipping take?")
    assert r.status_code == 200
    body = r.json()
    assert body["conversation_id"] == conversation_id
    assert body["message"]["role"] == "assistant"
    assert body["message"]["content"] == "Brand A ships within two business days."
    assert body["status"] == "ai_replied"
    assert set(body) == {"conversation_id", "message", "status"}  # nothing about retrieval/sources

    rows = messages_in_db(db)
    assert [(m.position, m.role, m.content) for m in rows] == [
        (1, MessageRole.USER, "How long does shipping take?"),
        (2, MessageRole.ASSISTANT, "Brand A ships within two business days."),
    ]
    assert {m.organization_id for m in rows} == {keys.a_org}
    assert rows[1].meta["grounded"] is True and rows[1].meta["sources"][0]["url"].endswith(
        "/shipping"
    )
    assert rows[0].meta is None
    db.expire_all()
    assert db.get(Conversation, uuid.UUID(conversation_id)).message_count == 2


def test_the_llm_receives_this_organizations_knowledge_and_business_name(client, keys, chat, llm):
    conversation_id, token = open_chat(client, keys.a)
    say(client, keys.a, conversation_id, token, "How long does shipping take?")
    sent = "\n".join(m.content for m in llm.calls[0])
    assert "Brand A ships within two business days" in sent and "Brand A" in llm.calls[0][0].content
    assert "secret wholesale" not in sent  # organization B's knowledge is not in A's prompt


def test_unanswerable_questions_get_the_fallback_and_never_reach_the_llm(
    client, keys, chat, llm, search
):
    search.by_org[keys.a_org] = []  # nothing relevant in the knowledge base
    conversation_id, token = open_chat(client, keys.a)
    r = say(client, keys.a, conversation_id, token, "Do you sell motorcycles?")
    assert r.status_code == 200
    assert "don't have enough information" in r.json()["message"]["content"]
    assert llm.calls == []


def test_follow_up_questions_carry_the_conversation_context(client, keys, chat, llm, search):
    from tests.chat.helpers import make_hit

    search.by_org[keys.a_org] = [make_hit("Oslo hoodie in black: 59 dollars.", score=0.9)]
    llm.replies = ["Yes, the black Oslo hoodie.", "It's 59 dollars."]
    conversation_id, token = open_chat(client, keys.a)
    say(client, keys.a, conversation_id, token, "Do you have black hoodies?")
    say(client, keys.a, conversation_id, token, "What's the price of that one?")

    second_call = llm.calls[1]
    assert [m.role for m in second_call] == ["system", "user", "assistant", "system", "user"]
    assert second_call[1].content == "Do you have black hoodies?"
    assert second_call[2].content == "Yes, the black Oslo hoodie."
    assert second_call[-1].content == "What's the price of that one?"
    # retrieval for the follow-up also used the previous turn
    assert "black hoodies" in chat._embedder.inputs[1][0]


def test_history_sent_to_the_llm_is_limited(client, keys, chat, llm, app_settings):
    app_settings.chat_history_limit = 4
    conversation_id, token = open_chat(client, keys.a)
    for i in range(6):
        assert (
            say(client, keys.a, conversation_id, token, f"question number {i}").status_code == 200
        )
    last_call = llm.calls[-1]
    history = [m.content for m in last_call[1:-2]]
    assert len(history) == 4 and history[-1].startswith("Here is what I found")
    assert "question number 0" not in "\n".join(m.content for m in last_call)


def test_conversation_history_can_be_read_back_by_its_owner(client, keys, chat, llm):
    llm.replies = ["First answer.", "Second answer."]
    conversation_id, token = open_chat(client, keys.a)
    say(client, keys.a, conversation_id, token, "First question")
    say(client, keys.a, conversation_id, token, "Second question")
    r = client.get(
        f"/api/public/chat/{keys.a}/conversations/{conversation_id}",
        headers={"X-Conversation-Token": token},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["conversation_id"] == conversation_id and body["status"] == "OPEN"
    assert [(m["role"], m["content"]) for m in body["messages"]] == [
        ("user", "First question"),
        ("assistant", "First answer."),
        ("user", "Second question"),
        ("assistant", "Second answer."),
    ]
    assert all(
        set(m) == {"role", "content", "created_at"} for m in body["messages"]
    )  # no ids, no meta


def test_system_messages_are_never_returned_to_visitors(client, keys, chat, llm, db):
    llm.replies = ["An answer."]
    conversation_id, token = open_chat(client, keys.a)
    say(client, keys.a, conversation_id, token, "A shipping question")
    conversation = db.get(Conversation, uuid.UUID(conversation_id))
    db.add(
        Message(
            organization_id=conversation.organization_id,
            conversation_id=conversation.id,
            position=99,
            role=MessageRole.SYSTEM,
            content="internal note: do not show",
        )
    )
    db.commit()
    r = client.get(
        f"/api/public/chat/{keys.a}/conversations/{conversation_id}",
        headers={"X-Conversation-Token": token},
    )
    assert r.status_code == 200
    assert [m["role"] for m in r.json()["messages"]] == ["user", "assistant"]
    assert "internal note" not in r.text


def test_an_empty_conversation_reads_back_empty(client, keys, chat):
    conversation_id, token = open_chat(client, keys.a)
    r = client.get(
        f"/api/public/chat/{keys.a}/conversations/{conversation_id}",
        headers={"X-Conversation-Token": token},
    )
    assert r.json()["messages"] == []


# --- the public API cannot be used to reach other organizations -------------------------------------------------------


def test_organization_ids_in_the_request_are_ignored(client, keys, chat, db):
    forged = {"organization_id": str(keys.b_org), "channel": "SMS", "status": "CLOSED"}
    r = client.post(f"/api/public/chat/{keys.a}/conversations", json=forged)
    assert r.status_code == 201
    conversation_id, token = r.json()["conversation_id"], r.json()["token"]
    r = client.post(
        f"/api/public/chat/{keys.a}/messages",
        json={"conversation_id": conversation_id, "content": "hello shipping", **forged},
        headers={"X-Conversation-Token": token},
    )
    assert r.status_code == 200
    db.expire_all()
    conversation = db.get(Conversation, uuid.UUID(conversation_id))
    assert conversation.organization_id == keys.a_org
    assert (
        conversation.channel == ConversationChannel.WEBSITE
        and conversation.status == ConversationStatus.OPEN
    )
    assert {m.organization_id for m in messages_in_db(db)} == {keys.a_org}


def test_another_organizations_key_cannot_read_or_write_my_conversation(client, keys, chat, db):
    conversation_id, token = open_chat(client, keys.a)
    say(client, keys.a, conversation_id, token, "my private question about shipping")

    read = client.get(
        f"/api/public/chat/{keys.b}/conversations/{conversation_id}",
        headers={"X-Conversation-Token": token},
    )
    write = say(client, keys.b, conversation_id, token, "intruder")
    assert read.status_code == write.status_code == 404
    assert "private question" not in read.text
    assert len([m for m in messages_in_db(db) if m.content == "intruder"]) == 0


def test_knowing_the_conversation_id_is_not_enough(client, keys, chat):
    conversation_id, token = open_chat(client, keys.a)
    say(client, keys.a, conversation_id, token, "a secret question about shipping")
    url = f"/api/public/chat/{keys.a}/conversations/{conversation_id}"
    for headers in (
        {},
        {"X-Conversation-Token": ""},
        {"X-Conversation-Token": "wrong-token"},
        {"X-Conversation-Token": token[:-1]},
    ):
        assert client.get(url, headers=headers).status_code == 404
    assert (
        client.post(
            f"/api/public/chat/{keys.a}/messages",
            json={"conversation_id": conversation_id, "content": "hi"},
        ).status_code
        == 404
    )


def test_one_visitors_token_does_not_open_another_visitors_conversation(client, keys, chat):
    first_id, first_token = open_chat(client, keys.a, visitor_id="visitor-one-1234")
    second_id, second_token = open_chat(client, keys.a, visitor_id="visitor-two-1234")
    assert say(client, keys.a, first_id, second_token, "swap").status_code == 404
    assert say(client, keys.a, second_id, first_token, "swap").status_code == 404
    assert say(client, keys.a, first_id, first_token, "mine about shipping").status_code == 200


def test_foreign_and_nonexistent_conversations_are_indistinguishable(client, keys, chat):
    foreign_id, foreign_token = open_chat(client, keys.b)
    foreign = client.get(
        f"/api/public/chat/{keys.a}/conversations/{foreign_id}",
        headers={"X-Conversation-Token": foreign_token},
    )
    missing = client.get(
        f"/api/public/chat/{keys.a}/conversations/{uuid.uuid4()}",
        headers={"X-Conversation-Token": foreign_token},
    )
    assert foreign.status_code == missing.status_code == 404 and foreign.json() == missing.json()


def test_cross_tenant_knowledge_never_appears_in_replies(client, keys, chat, llm):
    """Organization A asks about exactly what only organization B knows."""
    conversation_id, token = open_chat(client, keys.a)
    say(client, keys.a, conversation_id, token, "What is your secret wholesale price list?")
    sent = "\n".join(m.content for call in llm.calls for m in call)
    assert "wholesale price list: 3 dollars" not in sent
    # while B can reach it
    b_id, b_token = open_chat(client, keys.b)
    say(client, keys.b, b_id, b_token, "What is your secret wholesale price list?")
    assert "3 dollars per bar" in "\n".join(m.content for m in llm.calls[-1])


# --- unknown keys and enumeration ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        "pk_" + "A" * 32,
        "pk_short",
        "",
        "nokeyprefix" + "x" * 30,
        "pk_" + "A" * 200,
        "pk_%00evil",
        "PK_" + "a" * 32,
        "1; DROP TABLE",
    ],
)
def test_unknown_malformed_or_foreign_keys_all_look_the_same(client, keys, chat, key):
    results = [
        client.get(f"/api/public/chat/{key}/config"),
        client.post(f"/api/public/chat/{key}/conversations", json={}),
        client.post(
            f"/api/public/chat/{key}/messages",
            json={"conversation_id": str(uuid.uuid4()), "content": "x"},
        ),
    ]
    reference = client.get("/api/public/chat/pk_" + "Z" * 32 + "/config")
    for r in results:
        assert r.status_code in (404, 405, 307, 422)
    assert reference.status_code == 404 and reference.json() == {"detail": "Chat is not available"}
    assert not any(str(keys.a_org) in r.text or "Brand" in r.text for r in results)


def test_a_disabled_widget_is_indistinguishable_from_an_unknown_key(client, keys, chat):
    client.patch("/api/chat/widget", json={"enabled": False}, headers=keys.a_headers)
    disabled = client.get(f"/api/public/chat/{keys.a}/config")
    unknown = client.get("/api/public/chat/pk_" + "Q" * 32 + "/config")
    assert disabled.status_code == unknown.status_code == 404 and disabled.json() == unknown.json()


def test_public_endpoints_need_no_login_and_ignore_dashboard_credentials(client, keys, chat):
    # a dashboard token for organization B sent to A's public chat grants nothing and changes nothing
    conversation_id, token = open_chat(client, keys.a, headers=keys.b_headers)
    r = say(client, keys.a, conversation_id, token, "shipping?", headers=keys.b_headers)
    assert r.status_code == 200
    # and there is no way to list organizations or conversations through the public API
    assert client.get(f"/api/public/chat/{keys.a}/conversations").status_code in (404, 405)
    assert client.get("/api/public/chat").status_code in (404, 405)


# --- message validation ---------------------------------------------------------------------------------------------------------


def test_empty_and_whitespace_messages_are_rejected(client, keys, chat, llm, db):
    conversation_id, token = open_chat(client, keys.a)
    for text in ("", "   ", "\n\t \n", "\x00\x01"):
        assert say(client, keys.a, conversation_id, token, text).status_code == 422
    assert llm.calls == [] and messages_in_db(db) == []


def test_oversized_messages_are_rejected_without_calling_the_llm(
    client, keys, chat, llm, db, app_settings
):
    conversation_id, token = open_chat(client, keys.a)
    too_long = "x" * (app_settings.chat_max_message_chars + 1)
    r = say(client, keys.a, conversation_id, token, too_long)
    assert r.status_code == 422 and "at most" in r.json()["detail"]
    assert (
        say(client, keys.a, conversation_id, token, "y" * 12_000).status_code == 422
    )  # schema ceiling
    assert say(client, keys.a, conversation_id, token, "y" * 20_000).status_code == 413  # body cap
    assert llm.calls == [] and messages_in_db(db) == []
    assert (
        say(
            client, keys.a, conversation_id, token, "x" * app_settings.chat_max_message_chars
        ).status_code
        == 200
    )


def test_messages_are_trimmed_and_cleaned(client, keys, chat, db):
    conversation_id, token = open_chat(client, keys.a)
    say(client, keys.a, conversation_id, token, "  shipping\x00 \r\n\r\n\r\n\r\n question  \n")
    assert messages_in_db(db)[0].content == "shipping \n\n question"


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"content": "hi"},
        {"conversation_id": "nope", "content": "hi"},
        {"conversation_id": str(uuid.uuid4())},
        {"conversation_id": str(uuid.uuid4()), "content": 5},
    ],
)
def test_malformed_message_requests_are_rejected(client, keys, chat, body):
    assert (
        client.post(
            f"/api/public/chat/{keys.a}/messages", json=body, headers={"X-Conversation-Token": "t"}
        ).status_code
        == 422
    )


@pytest.mark.parametrize(
    "visitor_id",
    ["short", "has spaces in it", "x" * 65, "bad/chars/here", "<script>alert(1)</script>"],
)
def test_invalid_visitor_ids_are_rejected(client, keys, chat, visitor_id):
    assert (
        client.post(
            f"/api/public/chat/{keys.a}/conversations", json={"visitor_id": visitor_id}
        ).status_code
        == 422
    )


def test_html_and_script_text_is_stored_and_returned_as_inert_plain_text(
    client, keys, chat, db, llm
):
    payload = '<script>alert(1)</script><img src=x onerror=alert(2)> & "quotes"'
    llm.replies = ["<b>Bold</b> reply with <script>x()</script>"]
    conversation_id, token = open_chat(client, keys.a)
    r = say(client, keys.a, conversation_id, token, payload)
    assert r.headers["content-type"].startswith("application/json")  # never text/html
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.json()["message"]["content"] == "<b>Bold</b> reply with <script>x()</script>"
    assert (
        messages_in_db(db)[0].content == payload
    )  # stored verbatim as text, not escaped or executed


# --- turn semantics ---------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "code",
    ["auth", "timeout", "rate_limited", "unavailable", "provider_error", "model_unavailable"],
)
def test_provider_failures_return_a_safe_503_keep_the_customers_message_and_unlock(
    client, keys, chat, llm, db, code
):
    llm.fail_with = LLMError(code, "internal detail with sk-SECRET-KEY and a stack trace")
    conversation_id, token = open_chat(client, keys.a)
    r = say(client, keys.a, conversation_id, token, "How long does shipping take?")
    assert r.status_code == 503 and r.json() == {"detail": UNAVAILABLE}
    for leak in ("SECRET", "internal detail", "stack", "Traceback", "llm_", "provider"):
        assert leak not in r.text
    assert [(m.role, m.content) for m in messages_in_db(db)] == [
        (MessageRole.USER, "How long does shipping take?")
    ]
    db.expire_all()
    assert db.get(Conversation, uuid.UUID(conversation_id)).locked_until is None
    llm.fail_with = None  # the provider recovers; the same conversation keeps working
    assert (
        say(client, keys.a, conversation_id, token, "How long does shipping take?").status_code
        == 200
    )


def test_retrying_after_an_outage_does_not_confuse_the_model_with_duplicate_turns(
    client, keys, chat, llm
):
    llm.fail_with = LLMError("timeout")
    conversation_id, token = open_chat(client, keys.a)
    say(client, keys.a, conversation_id, token, "shipping time?")
    llm.fail_with = None
    say(client, keys.a, conversation_id, token, "shipping time?")
    roles = [m.role for m in llm.calls[-1]]
    assert roles == [
        "system",
        "system",
        "user",
    ]  # the unanswered earlier attempt was collapsed away


def test_a_missing_api_key_is_a_controlled_503_with_no_secrets(client, keys, db):
    # no test agent installed: the real configuration path runs, with openai selected and no key
    from app.core.config import get_settings
    from app.main import app

    settings = get_settings().model_copy(
        update={"llm_provider": "openai", "openai_api_key": None, "crawl_min_interval_seconds": 0}
    )
    app.dependency_overrides[get_settings] = lambda: settings
    conversation_id, token = open_chat(client, keys.a)
    r = say(client, keys.a, conversation_id, token, "hello")
    assert r.status_code == 503 and r.json() == {"detail": UNAVAILABLE}
    assert "OPENAI" not in r.text and "key" not in r.text.lower()


def test_embedding_failure_is_a_safe_503(client, keys, chat):
    from tests.chat.helpers import SpyEmbedder

    chat._embedder = SpyEmbedder(fail=True)
    conversation_id, token = open_chat(client, keys.a)
    r = say(client, keys.a, conversation_id, token, "shipping?")
    assert r.status_code == 503 and r.json() == {"detail": UNAVAILABLE}


def test_only_one_reply_is_generated_at_a_time_per_conversation(client, keys, chat, llm, db):
    conversation_id, token = open_chat(client, keys.a)
    nested = {}

    def second_request_while_first_is_in_flight():
        if "response" not in nested:
            nested["response"] = say(
                client, keys.a, conversation_id, token, "impatient double click"
            )

    llm.hook = second_request_while_first_is_in_flight
    first = say(client, keys.a, conversation_id, token, "How long does shipping take?")
    assert first.status_code == 200
    assert nested["response"].status_code == 409 and "wait" in nested["response"].json()["detail"]
    assert [m.content for m in messages_in_db(db) if m.role == MessageRole.USER] == [
        "How long does shipping take?"
    ]


def test_a_stale_lock_from_a_crashed_request_does_not_wedge_the_conversation(
    client, keys, chat, db
):
    conversation_id, token = open_chat(client, keys.a)
    row = db.get(Conversation, uuid.UUID(conversation_id))
    row.locked_until = datetime.now(UTC) + timedelta(seconds=60)
    db.commit()
    assert say(client, keys.a, conversation_id, token, "shipping?").status_code == 409
    row.locked_until = datetime.now(UTC) - timedelta(seconds=1)  # expired
    db.commit()
    assert say(client, keys.a, conversation_id, token, "shipping?").status_code == 200


def test_closed_conversations_do_not_accept_messages(client, keys, chat, db):
    conversation_id, token = open_chat(client, keys.a)
    db.get(Conversation, uuid.UUID(conversation_id)).status = ConversationStatus.CLOSED
    db.commit()
    assert say(client, keys.a, conversation_id, token, "hello").status_code == 409


def test_conversations_have_a_message_limit(client, keys, chat, app_settings):
    app_settings.chat_max_messages_per_conversation = 4
    conversation_id, token = open_chat(client, keys.a)
    assert say(client, keys.a, conversation_id, token, "one shipping").status_code == 200
    assert say(client, keys.a, conversation_id, token, "two shipping").status_code == 200
    r = say(client, keys.a, conversation_id, token, "three shipping")
    assert r.status_code == 409 and "limit" in r.json()["detail"]


def test_message_positions_stay_ordered_across_many_turns(client, keys, chat, db):
    conversation_id, token = open_chat(client, keys.a)
    for i in range(5):
        say(client, keys.a, conversation_id, token, f"shipping question {i}")
    rows = messages_in_db(db)
    assert [m.position for m in rows] == list(range(1, 11))
    assert [m.role for m in rows] == [MessageRole.USER, MessageRole.ASSISTANT] * 5


def test_a_different_message_after_an_outage_keeps_the_earlier_context(client, keys, chat, llm):
    llm.fail_with = LLMError("timeout")
    conversation_id, token = open_chat(client, keys.a)
    say(client, keys.a, conversation_id, token, "Do you have black hoodies?")
    llm.fail_with = None
    say(client, keys.a, conversation_id, token, "in size large?")
    contents = [m.content for m in llm.calls[-1]]
    assert "Do you have black hoodies?" in contents and contents[-1] == "in size large?"
