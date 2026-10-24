"""Verify the configured AI provider against the REAL service, end to end.

    cd backend
    python -m tools.ai_live_check              # chat + failure modes
    python -m tools.ai_live_check --embedding  # also the embedding provider and a full RAG answer

It uses exactly the configuration the API and worker use (``LLM_*`` / ``EMBEDDING_*`` and their
keys, from the environment or ``backend/.env``) and goes through the same registry, adapters and
chat agent as production, so a pass means the real path works for that vendor and model.

Checks (each prints the normalized outcome; nothing is printed that could contain a secret, a full
prompt or a full reply):

  1. a real chat request: model, latency, finish reason, token usage, vendor request id
  2. an invalid API key       -> must be reported as ``auth``
  3. an unreachable timeout   -> must be reported as ``timeout`` (or ``unavailable``)
  4. a model that does not exist -> reported as ``model_unavailable`` / ``invalid_request``
  5. (--embedding) a real embedding: width must equal the vector column, then the whole chat agent
     answers a grounded question from two tiny in-memory documents and refuses an unrelated one
     WITHOUT calling the model

Exit status is non-zero if the real request fails or an expected failure is mis-classified.
This is a live-test tool: it spends a few tokens of the configured account.
"""

import argparse
import math
import sys
import uuid
from collections.abc import Sequence

from pydantic import SecretStr

from app.core.config import Settings, get_settings
from app.services.ai import factory
from app.services.ai.agent import ChatAgent
from app.services.ai.catalog import BUILTIN_PROVIDERS
from app.services.ai.embeddings import embedding_input
from app.services.ai.errors import AIError, AIErrorCode
from app.services.ai.provider import AIRequest, ChatMessage
from app.services.rag.search import SearchHit

FAILURES: list[str] = []


def report(ok: bool, name: str, detail: str = "") -> None:
    if not ok:
        FAILURES.append(name)
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"   [{detail}]" if detail else ""))


def info(name: str, detail: str) -> None:
    print(f"INFO  {name}   [{detail}]")


def expect_error(name: str, accepted: set[AIErrorCode], run) -> None:
    try:
        run()
    except AIError as exc:
        report(exc.code in accepted, name, f"observed code={exc.code.value}")
    except Exception as exc:  # noqa: BLE001 - a non-normalized error is exactly what we look for
        report(False, name, f"NOT NORMALIZED: {type(exc).__name__}")
    else:
        report(False, name, "no error was raised")


def requires_key(provider_id: str) -> bool:
    return next(s for s in BUILTIN_PROVIDERS if s.id == provider_id).requires_api_key


class InMemorySearch:
    """The vector search the chat agent calls, over a handful of in-memory documents. It compares
    only vectors from the same embedding model, like the production search."""

    def __init__(self) -> None:
        self.rows: list[tuple[SearchHit, list[float], str]] = []

    def add(self, text: str, title: str, vector: Sequence[float], model_id: str) -> None:
        hit = SearchHit(
            chunk_id=uuid.uuid4(),
            document_id=uuid.uuid4(),
            source_id=uuid.uuid4(),
            url="https://example.invalid/" + title.lower().replace(" ", "-"),
            title=title,
            content=text,
            chunk_index=0,
            score=0.0,
        )
        self.rows.append((hit, list(vector), model_id))

    def __call__(self, db, **kwargs):
        query = kwargs["query_embedding"]
        model = kwargs["embedding_model"]
        found = []
        for hit, vector, vector_model in self.rows:
            if vector_model != model:
                continue
            dot = sum(a * b for a, b in zip(query, vector, strict=True))
            norm = math.sqrt(sum(a * a for a in query)) * math.sqrt(sum(b * b for b in vector))
            score = dot / norm if norm else 0.0
            if kwargs.get("min_score") is None or score >= kwargs["min_score"]:
                found.append(SearchHit(**{**hit.__dict__, "score": score}))
        found.sort(key=lambda h: h.score, reverse=True)
        return found[: kwargs.get("top_k", 5)]


def check_chat(settings: Settings) -> None:
    provider_id, model = settings.llm_provider, settings.llm_model or "(provider default)"
    print(
        f"\n== chat: provider={provider_id} model={model} base_url="
        f"{settings.llm_base_url or '(provider default)'}"
    )
    try:
        llm = factory.build_llm_provider(settings)
    except AIError as exc:
        report(False, "build the provider from configuration", f"code={exc.code.value}: {exc}")
        return
    request = AIRequest(
        messages=[
            ChatMessage("system", "Reply with exactly one word."),
            ChatMessage("user", "Say: pong"),
        ],
        max_tokens=32,
        temperature=0,
    )
    try:
        reply = llm.generate(request)
    except AIError as exc:
        report(False, "a real chat request", f"code={exc.code.value}: {exc}")
        return
    usage = reply.usage
    report(
        bool(reply.text.strip()), "a real chat request returned text", f"{len(reply.text)} chars"
    )
    info("provider / model served", f"{reply.provider} / {reply.model}")
    info("latency", f"{reply.latency_ms} ms")
    info("finish reason", str(reply.finish_reason))
    info(
        "token usage",
        "unreported"
        if usage is None
        else f"prompt={usage.prompt_tokens} completion={usage.completion_tokens} "
        f"total={usage.total_tokens}",
    )
    info("vendor request id", reply.request_id or "(none returned)")
    report(reply.latency_ms >= 0 and reply.provider == provider_id, "response is normalized")
    if provider_id == "fake":
        info("failure modes", "skipped: the fake provider has none (it is not a real service)")
        return

    if requires_key(provider_id):
        bad = settings.model_copy(update={"llm_api_key": SecretStr("bizflow-invalid-key-0000")})
        bad.openai_api_key = None  # the legacy fallback must not rescue the invalid key
        expect_error(
            "an invalid API key is reported as auth",
            {AIErrorCode.AUTH},
            lambda: factory.build_llm_provider(bad).generate(request),
        )
    else:
        info("invalid API key", "skipped: this provider does not require a key")
    short = settings.model_copy(update={"llm_timeout_seconds": 0.001})
    expect_error(
        "a request that cannot finish in time is reported as timeout",
        {AIErrorCode.TIMEOUT, AIErrorCode.UNAVAILABLE},
        lambda: factory.build_llm_provider(short).generate(request),
    )
    unknown = settings.model_copy(update={"llm_model": "bizflow-no-such-model-0000"})
    expect_error(
        "a model that does not exist is a normalized error",
        {AIErrorCode.MODEL_UNAVAILABLE, AIErrorCode.INVALID_REQUEST},
        lambda: factory.build_llm_provider(unknown).generate(request),
    )


def check_embedding_and_rag(settings: Settings) -> None:
    print(
        f"\n== embeddings: provider={settings.embedding_provider} "
        f"model={settings.embedding_model or '(provider default)'}"
    )
    try:
        embedder = factory.build_embedding_provider(settings)
        vectors = embedder.embed(["hello world"])
    except AIError as exc:
        report(False, "a real embedding request", f"code={exc.code.value}: {exc}")
        return
    report(
        len(vectors[0]) == embedder.dimensions,
        "vector width matches the database column",
        f"{len(vectors[0])} dimensions",
    )
    info("embedding model id (stored with every chunk)", embedder.model_id)
    if settings.embedding_provider == "fake":
        info("RAG", "skipped: the fake embedding provider is not semantic")
        return

    search = InMemorySearch()
    docs = [
        (
            "Shipping Policy",
            "Orders ship within two business days. Delivery takes five to seven days.",
        ),
        ("Returns Policy", "Unopened soap can be returned within thirty days for a full refund."),
    ]
    for title, text in docs:
        vector = embedder.embed([embedding_input(title, text)])[0]
        search.add(text, title, vector, embedder.model_id)

    try:
        llm = factory.build_llm_provider(settings)
    except AIError as exc:
        report(False, "build the chat provider for RAG", f"code={exc.code.value}")
        return
    agent = ChatAgent(llm=llm, embedder=embedder, settings=settings, search=search)
    org = uuid.uuid4()
    grounded = agent.respond(
        None,  # type: ignore[arg-type]
        organization_id=org,
        business_name="Luna Soap Co",
        question="How many business days until orders ship?",
        history=[],
    )
    report(
        grounded.grounded and bool(grounded.sources),
        "a question the documents answer is grounded",
        f"sources={len(grounded.sources)} "
        f"tokens={grounded.prompt_tokens}/{grounded.completion_tokens}",
    )
    report(
        "two" in grounded.content.lower() or "2" in grounded.content,
        "the answer uses the retrieved fact",
        f"{len(grounded.content)} chars",
    )
    other = agent.respond(
        None,  # type: ignore[arg-type]
        organization_id=org,
        business_name="Luna Soap Co",
        question="What is the capital of Mongolia and who won the 1998 football world cup?",
        history=[],
    )
    report(
        not other.grounded and other.model is None,
        "an unrelated question gets the fallback WITHOUT calling the model",
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--embedding", action="store_true", help="also check embeddings and RAG")
    args = parser.parse_args(argv)
    settings = get_settings()
    check_chat(settings)
    if args.embedding:
        check_embedding_and_rag(settings)
    print("\nALL CHECKS PASSED" if not FAILURES else f"\n{len(FAILURES)} CHECK(S) FAILED")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
