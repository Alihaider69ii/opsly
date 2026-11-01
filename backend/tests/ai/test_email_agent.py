import uuid

import pytest

from app.db.models import WhatsAppAIConfidence
from app.services.ai import prompts
from app.services.ai.email_agent import MAX_REPLY_CHARS, EmailAIUnavailable, EmailConversationAgent
from app.services.ai.provider import ChatMessage, LLMError
from tests.chat.helpers import RecordingLLM, SpyEmbedder, StubSearch, make_hit, settings_with

ORG = uuid.uuid4()
SHIPPING = make_hit(
    "Orders ship within two business days. Shipping is free over 50 dollars.", score=0.9
)


def make_email_agent(*, llm=None, embedder=None, search=None, **settings_overrides):
    return EmailConversationAgent(
        llm=llm or RecordingLLM(),
        embedder=embedder or SpyEmbedder(),
        settings=settings_with(**settings_overrides),
        search=search or StubSearch(),
    )


def ask(
    agent,
    question="How long does shipping take?",
    history=(),
    org=ORG,
    business="Acme",
    grounding_required=True,
):
    return agent.respond(
        None,  # type: ignore[arg-type]  (the stub search needs no database)
        organization_id=org,
        business_name=business,
        question=question,
        history=list(history),
        grounding_required=grounding_required,
    )


def test_grounded_answer_is_generated_and_uses_the_email_system_prompt():
    llm = RecordingLLM(["Orders ship within two business days, and shipping is free over $50."])
    reply = ask(make_email_agent(llm=llm, search=StubSearch({ORG: [SHIPPING]})))

    assert reply.grounded is True
    assert reply.is_greeting is False
    assert reply.confidence != WhatsAppAIConfidence.NONE
    assert reply.content == "Orders ship within two business days, and shipping is free over $50."
    (call,) = llm.calls
    system_prompt, question = call[0], call[-1]
    assert "support email" in system_prompt.content
    assert question == ChatMessage("user", "How long does shipping take?")


def test_customer_context_reaches_the_prompt_as_its_own_block():
    llm = RecordingLLM(["Sure, I can see your last order."])
    agent = make_email_agent(llm=llm, search=StubSearch({ORG: [SHIPPING]}))
    agent.respond(
        None,  # type: ignore[arg-type]
        organization_id=ORG,
        business_name="Acme",
        question="Where is my order?",
        history=[],
        customer_facts=["The customer's name on file is Jordan."],
    )
    (call,) = llm.calls
    sent = "\n".join(m.content for m in call)
    assert "Jordan" in sent and "CUSTOMER CONTEXT" in sent


def test_audit_records_grounding_and_confidence_but_not_chain_of_thought():
    reply = ask(
        make_email_agent(llm=RecordingLLM(["Two business days."]), search=StubSearch({ORG: [SHIPPING]}))
    )
    audit = reply.audit()["ai"]
    assert audit["grounded"] is True
    assert audit["confidence"] in {"MEDIUM", "HIGH", "LOW"}
    assert audit["sources_count"] == 1
    assert audit["prompt_version"] == prompts.PROMPT_VERSION
    assert "reasoning" not in audit and "chain_of_thought" not in audit


def test_no_relevant_knowledge_never_calls_the_llm_for_a_real_question():
    llm = RecordingLLM(["I would have made something up."])
    reply = ask(make_email_agent(llm=llm, search=StubSearch({ORG: []})), "Do you sell cars?")
    assert reply.grounded is False
    assert reply.confidence == WhatsAppAIConfidence.NONE
    assert reply.content == ""
    assert llm.calls == []


def test_a_greeting_reaches_the_llm_even_with_no_retrieved_knowledge():
    llm = RecordingLLM(["Hi there, thanks for reaching out!"])
    reply = ask(make_email_agent(llm=llm, search=StubSearch({ORG: []})), "hi")
    assert len(llm.calls) == 1
    assert reply.is_greeting is True
    assert reply.grounded is False
    assert reply.content == "Hi there, thanks for reaching out!"


def test_grounding_required_false_still_calls_the_llm_with_zero_retrieved_knowledge():
    llm = RecordingLLM(["I'm not sure, but happy to help however I can."])
    reply = ask(
        make_email_agent(llm=llm, search=StubSearch({ORG: []})),
        "Do you offer gift wrapping?",
        grounding_required=False,
    )
    assert len(llm.calls) == 1
    assert reply.grounded is False
    assert reply.is_greeting is False
    assert reply.content == "I'm not sure, but happy to help however I can."


def test_weak_matches_below_the_threshold_trigger_the_fallback_path():
    llm = RecordingLLM()
    reply = ask(
        make_email_agent(
            llm=llm, search=StubSearch({ORG: [make_hit("Barely related.", score=0.1)]}), rag_similarity_threshold=0.3
        )
    )
    assert reply.grounded is False and llm.calls == []


def test_llm_failure_raises_email_ai_unavailable():
    llm = RecordingLLM(fail_with=LLMError("provider_error", "boom"))
    agent = make_email_agent(llm=llm, search=StubSearch({ORG: [SHIPPING]}))
    with pytest.raises(EmailAIUnavailable) as exc:
        ask(agent)
    assert exc.value.reason == "llm_provider_error"


def test_embedding_failure_raises_email_ai_unavailable():
    embedder = SpyEmbedder(fail=True)
    agent = make_email_agent(embedder=embedder, search=StubSearch({ORG: [SHIPPING]}))
    with pytest.raises(EmailAIUnavailable) as exc:
        ask(agent)
    assert exc.value.reason == "embedding"


def test_reply_is_truncated_to_the_emails_own_longer_character_budget():
    """Email tolerates a far longer reply than WhatsApp's 1000-char budget (brief: email allows a
    full, multi-sentence reply)."""
    long_reply = "x" * (MAX_REPLY_CHARS + 500)
    llm = RecordingLLM([long_reply])
    reply = ask(make_email_agent(llm=llm, search=StubSearch({ORG: [SHIPPING]})))
    assert len(reply.content) == MAX_REPLY_CHARS


def test_another_organizations_knowledge_never_reaches_the_llm():
    other = uuid.uuid4()
    llm = RecordingLLM()
    search = StubSearch({ORG: [SHIPPING], other: [make_hit("Other brand's secret formula.", score=0.99)]})
    reply = ask(make_email_agent(llm=llm, search=search), org=ORG)
    assert reply.grounded
    sent = "\n".join(m.content for call in llm.calls for m in call)
    assert "secret formula" not in sent
