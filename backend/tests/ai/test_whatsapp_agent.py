import uuid

import pytest

from app.db.models import WhatsAppAIConfidence
from app.services.ai import prompts
from app.services.ai.provider import ChatMessage, LLMError
from app.services.ai.whatsapp_agent import (
    WhatsAppAIUnavailable,
    WhatsAppConversationAgent,
    classify_confidence,
    is_greeting,
    meets_minimum,
    requests_human,
    validate_reply,
)
from tests.chat.helpers import RecordingLLM, SpyEmbedder, StubSearch, make_hit, settings_with

ORG = uuid.uuid4()
SHIPPING = make_hit(
    "Orders ship within two business days. Shipping is free over 50 dollars.", score=0.9
)


def make_whatsapp_agent(*, llm=None, embedder=None, search=None, **settings_overrides):
    return WhatsAppConversationAgent(
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


# --- confidence classification (deterministic, never model-reported) ---------------------------


def test_confidence_is_none_without_any_sources():
    assert classify_confidence([], similarity_threshold=0.25) == WhatsAppAIConfidence.NONE


def test_confidence_is_low_just_above_threshold():
    hit = make_hit("x", score=0.30)  # margin 0.05
    assert classify_confidence([hit], similarity_threshold=0.25) == WhatsAppAIConfidence.LOW


def test_confidence_is_medium_with_a_moderate_margin():
    hit = make_hit("x", score=0.45)  # margin 0.20
    assert classify_confidence([hit], similarity_threshold=0.25) == WhatsAppAIConfidence.MEDIUM


def test_confidence_is_high_only_with_a_strong_margin_and_multiple_sources():
    strong = [make_hit("a", score=0.70), make_hit("b", score=0.65)]  # margin 0.45, two sources
    assert classify_confidence(strong, similarity_threshold=0.25) == WhatsAppAIConfidence.HIGH
    # A single strong source alone is not enough for HIGH -- corroboration matters too.
    assert classify_confidence(strong[:1], similarity_threshold=0.25) == WhatsAppAIConfidence.MEDIUM
    # Two sources alone are not enough either -- the margin must ALSO be strong.
    moderate = [make_hit("a", score=0.45), make_hit("b", score=0.40)]  # margin 0.20, two sources
    assert classify_confidence(moderate, similarity_threshold=0.25) == WhatsAppAIConfidence.MEDIUM


def test_meets_minimum_is_a_simple_rank_comparison():
    assert meets_minimum(WhatsAppAIConfidence.HIGH, WhatsAppAIConfidence.LOW)
    assert meets_minimum(WhatsAppAIConfidence.MEDIUM, WhatsAppAIConfidence.MEDIUM)
    assert not meets_minimum(WhatsAppAIConfidence.LOW, WhatsAppAIConfidence.MEDIUM)
    assert not meets_minimum(WhatsAppAIConfidence.NONE, WhatsAppAIConfidence.LOW)


# --- greeting / human-request detection (deterministic, documented scope) -----------------------


def test_greetings_and_closings_are_recognized():
    # English
    for text in [
        "hi",
        "Hello!",
        "hey there",
        "thanks",
        "thank you",
        "ok",
        "bye",
        "yes",
        "GOOD MORNING",
    ]:
        assert is_greeting(text), text
    # Hindi / Hinglish / Devanagari
    for text in [
        "namaste",
        "Namaskar!",
        "pranam",
        "salaam",
        "kaise ho?",
        "kya haal hai",
        "shukriya",
        "dhanyawad",
        "alvida",
        "नमस्ते",
        "नमस्कार!",
        "प्रणाम",
        "शुक्रिया",
        "धन्यवाद",
        "हैलो",
    ]:
        assert is_greeting(text), text


def test_a_real_question_is_not_a_greeting():
    assert not is_greeting("Do you have this in size 10?")
    assert not is_greeting("hi, do you ship to Canada?")  # a greeting PLUS a real question
    assert not is_greeting("namaste, what is the price?")
    assert not is_greeting("kaise ho, mera order kahan hai?")
    assert not is_greeting("नमस्ते, मुझे रिफंड चाहिए")


def test_requests_human_matches_common_phrasings():
    # English common phrasings
    for text in [
        "I want to talk to a human",
        "can I speak to an agent",
        "get me your customer service",
        "I need a real person",
        "let me talk to your manager",
        "customer care",
        "human",
        "agent",
        "representative",
        "speak to someone",
    ]:
        assert requests_human(text), text

    # Hindi / Hinglish patterns (including brief's required patterns)
    for text in [
        "mujhe agent se baat karni hai",
        "kisi person se baat karni hai",
        "insaan se baat karni hai",
        "customer care se baat karni hai",
        "aadmi se baat karni hai",
        "human se baat karni hai",
        "agent chahiye",
        "support chahiye",
        "kisi se baat karni hai",
        "executive se baat karni hai",
        "mujhe support chahiye",
    ]:
        assert requests_human(text), text

    # Devanagari patterns
    for text in [
        "मुझे एजेंट से बात करनी है",
        "किसी इंसान से बात करनी है",
        "एजेंट चाहिए",
        "कस्टमर केयर से बात करनी है",
        "बात करनी है",
    ]:
        assert requests_human(text), text


def test_requests_human_case_variations_punctuation_and_mixed_text():
    # Case variations
    assert requests_human("AGENT CHAHIYE")
    assert requests_human("Mujhe Agent Se Baat Karni Hai")
    assert requests_human("INSAAN SE BAAT KARNI HAI")
    assert requests_human("CUSTOMER CARE")

    # Punctuation
    assert requests_human("agent chahiye???")
    assert requests_human("kisi person se baat karni hai...")
    assert requests_human("human!")
    assert requests_human("talk to someone, please.")

    # Mixed text
    assert requests_human("Hello! Mujhe agent se baat karni hai please.")
    assert requests_human("Hi, can you connect me? I want to talk to a human.")


def test_requests_human_does_not_match_an_ordinary_question():
    # English ordinary questions
    assert not requests_human("Do you have this in size 10?")
    assert not requests_human("What is your refund policy?")
    assert not requests_human("How do I track my order?")
    assert not requests_human("Do you deliver to Mumbai?")
    assert not requests_human("Can I cancel my order #1234?")
    assert not requests_human("Is shipping free?")

    # Hindi / Hinglish ordinary questions
    assert not requests_human("Mera order kab aayega?")
    assert not requests_human("Order cancel kaise karein?")
    assert not requests_human("Product available hai kya?")
    assert not requests_human("Payment ho gaya lekin order confirm nahi hua")
    assert not requests_human("Size chart kahan milega?")
    assert not requests_human("Return pickup kab hoga?")
    assert not requests_human("Discount code kaam nahi kar raha")


# --- validate_reply: canary / tag leakage never reaches the customer ----------------------------


def test_validate_reply_rejects_canary_leakage():
    assert validate_reply(f"my rules say {prompts.PROMPT_CANARY}", 1000) is None


def test_validate_reply_rejects_knowledge_tag_leakage():
    assert validate_reply(f"<{prompts.KNOWLEDGE_TAG}>leaked</...>", 1000) is None


def test_validate_reply_rejects_customer_context_tag_leakage():
    assert validate_reply(f"<{prompts.CUSTOMER_CONTEXT_TAG}>leaked</...>", 1000) is None


def test_validate_reply_truncates_to_the_configured_limit():
    assert validate_reply("x" * 2000, 50) == "x" * 50


def test_validate_reply_rejects_empty_output():
    assert validate_reply("   \x00  ", 1000) is None


# --- full pipeline: grounded answers -------------------------------------------------------------


def test_grounded_answer_is_generated_from_retrieved_knowledge():
    llm = RecordingLLM(["Orders ship within two business days, and shipping is free over $50."])
    search = StubSearch({ORG: [SHIPPING]})
    reply = ask(make_whatsapp_agent(llm=llm, search=search))

    assert reply.grounded is True
    assert reply.is_greeting is False
    assert reply.confidence != WhatsAppAIConfidence.NONE
    assert reply.content == "Orders ship within two business days, and shipping is free over $50."
    (call,) = llm.calls
    system_prompt, question = call[0], call[-1]
    assert "WhatsApp" in system_prompt.content
    assert question == ChatMessage("user", "How long does shipping take?")


def test_customer_context_reaches_the_prompt_as_its_own_block():
    llm = RecordingLLM(["Sure, I can see your last order."])
    agent = make_whatsapp_agent(llm=llm, search=StubSearch({ORG: [SHIPPING]}))
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
        make_whatsapp_agent(
            llm=RecordingLLM(["Two business days."]), search=StubSearch({ORG: [SHIPPING]})
        )
    )
    audit = reply.audit()["ai"]
    assert audit["grounded"] is True
    assert audit["confidence"] in {"MEDIUM", "HIGH", "LOW"}
    assert audit["sources_count"] == 1
    assert audit["prompt_version"] == prompts.PROMPT_VERSION
    assert "reasoning" not in audit and "chain_of_thought" not in audit


# --- no relevant knowledge: fallback, no LLM call, unless it's a greeting -----------------------


def test_no_relevant_knowledge_never_calls_the_llm_for_a_real_question():
    llm = RecordingLLM(["I would have made something up."])
    reply = ask(make_whatsapp_agent(llm=llm, search=StubSearch({ORG: []})), "Do you sell cars?")
    assert reply.grounded is False
    assert reply.confidence == WhatsAppAIConfidence.NONE
    assert reply.content == ""
    assert llm.calls == []  # never consulted -- cannot hallucinate


def test_a_greeting_reaches_the_llm_even_with_no_retrieved_knowledge():
    llm = RecordingLLM(["Hi there! How can I help you today?"])
    reply = ask(make_whatsapp_agent(llm=llm, search=StubSearch({ORG: []})), "hi")
    assert len(llm.calls) == 1  # the model WAS consulted for a bare greeting
    assert reply.is_greeting is True
    assert reply.grounded is False  # still not "grounded in business knowledge"
    assert reply.content == "Hi there! How can I help you today?"
    (call,) = llm.calls
    sent = "\n".join(m.content for m in call)
    assert f"<{prompts.KNOWLEDGE_TAG}" not in sent  # no knowledge block when nothing was retrieved


def test_grounding_required_false_still_calls_the_llm_with_zero_retrieved_knowledge():
    """The real effect of the grounding_required=False parameter: without it, a real (non-
    greeting) question with no retrieval would never reach the model at all -- see
    test_no_relevant_knowledge_never_calls_the_llm_for_a_real_question above."""
    llm = RecordingLLM(["I'm not sure, but happy to help however I can."])
    reply = ask(
        make_whatsapp_agent(llm=llm, search=StubSearch({ORG: []})),
        "Do you offer gift wrapping?",
        grounding_required=False,
    )
    assert len(llm.calls) == 1
    assert reply.grounded is False  # no real knowledge backed this, even though it was sent
    assert reply.is_greeting is False  # it is a real question, not a greeting
    assert reply.content == "I'm not sure, but happy to help however I can."
    (call,) = llm.calls
    sent = "\n".join(m.content for m in call)
    assert f"<{prompts.KNOWLEDGE_TAG}" not in sent  # still no knowledge block to fabricate from


def test_weak_matches_below_the_threshold_trigger_the_fallback_path():
    llm = RecordingLLM()
    reply = ask(
        make_whatsapp_agent(
            llm=llm,
            search=StubSearch({ORG: [make_hit("Barely related.", score=0.1)]}),
            rag_similarity_threshold=0.3,
        )
    )
    assert reply.grounded is False and llm.calls == []


# --- provider / embedding failures surface as WhatsAppAIUnavailable -----------------------------


def test_llm_failure_raises_whatsapp_ai_unavailable():
    llm = RecordingLLM(fail_with=LLMError("provider_error", "boom"))
    agent = make_whatsapp_agent(llm=llm, search=StubSearch({ORG: [SHIPPING]}))
    with pytest.raises(WhatsAppAIUnavailable) as exc:
        ask(agent)
    assert exc.value.reason == "llm_provider_error"


def test_embedding_failure_raises_whatsapp_ai_unavailable():
    embedder = SpyEmbedder(fail=True)
    agent = make_whatsapp_agent(embedder=embedder, search=StubSearch({ORG: [SHIPPING]}))
    with pytest.raises(WhatsAppAIUnavailable) as exc:
        ask(agent)
    assert exc.value.reason == "embedding"


# --- tenant isolation (reuses the same RAG scoping proven in tests/ai/test_agent.py) -------------


def test_another_organizations_knowledge_never_reaches_the_llm():
    other = uuid.uuid4()
    llm = RecordingLLM()
    search = StubSearch(
        {ORG: [SHIPPING], other: [make_hit("Other brand's secret formula.", score=0.99)]}
    )
    reply = ask(make_whatsapp_agent(llm=llm, search=search), org=ORG)
    assert reply.grounded
    sent = "\n".join(m.content for call in llm.calls for m in call)
    assert "secret formula" not in sent
