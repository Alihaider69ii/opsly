import uuid
from collections.abc import Callable, Sequence

from app.core.config import Settings, get_settings
from app.services.ai.agent import ChatAgent
from app.services.ai.embeddings import EmbeddingError
from app.services.ai.provider import AIRequest, AIResponse, AIUsage, ChatMessage, LLMError
from app.services.ai.providers.fake import FakeEmbeddingProvider, FakeLLMProvider
from app.services.rag.search import SearchHit


def make_hit(
    content: str,
    *,
    url: str = "https://shop.example/shipping",
    title: str | None = "Shipping Policy",
    score: float = 0.8,
    index: int = 0,
) -> SearchHit:
    return SearchHit(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        source_id=uuid.uuid4(),
        url=url,
        title=title,
        content=content,
        chunk_index=index,
        score=score,
    )


class RecordingLLM(FakeLLMProvider):
    """Fake LLM that records every call and can be scripted to answer or fail."""

    def __init__(
        self,
        replies: Sequence[str] | None = None,
        *,
        fail_with: LLMError | None = None,
        hook: Callable[[], None] | None = None,
    ) -> None:
        self.calls: list[list[ChatMessage]] = []  # the messages of each call
        self.requests: list[AIRequest] = []
        self.replies = list(replies or [])
        self.fail_with = fail_with
        self.hook = hook

    def generate(self, request):
        self.calls.append(list(request.messages))
        self.requests.append(request)
        if self.hook is not None:
            self.hook()
        if self.fail_with is not None:
            raise self.fail_with
        if self.replies:
            return AIResponse(
                text=self.replies.pop(0),
                provider="fake",
                model="test-model",
                finish_reason="stop",
                usage=AIUsage.of(prompt_tokens=11, completion_tokens=7),
                request_id="req-test",
            )
        return super().generate(request)


class SpyEmbedder(FakeEmbeddingProvider):
    def __init__(self, fail: bool = False) -> None:
        super().__init__()
        self.inputs: list[list[str]] = []
        self.fail = fail

    def embed(self, texts):
        self.inputs.append(list(texts))
        if self.fail:
            raise EmbeddingError("unavailable", "boom")
        return super().embed(texts)


class StubSearch:
    """Stands in for the Postgres vector search in the SQLite unit tier. Hits are registered per
    organization, so a call for organization X can only ever see X's hits, mirroring the real
    scoping guarantee that the integration tier verifies against pgvector."""

    def __init__(self, by_org: dict[uuid.UUID, list[SearchHit]] | None = None) -> None:
        self.by_org = by_org or {}
        self.calls: list[dict] = []
        self.error: Exception | None = None

    def __call__(self, db, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        hits = self.by_org.get(kwargs["organization_id"], [])
        min_score = kwargs.get("min_score")
        top_k = kwargs.get("top_k", 5)
        return [h for h in hits if min_score is None or h.score >= min_score][:top_k]


def settings_with(**overrides) -> Settings:
    return get_settings().model_copy(update={"llm_provider": "fake", **overrides})


def make_agent(
    *,
    llm=None,
    embedder=None,
    search=None,
    **settings_overrides,
) -> ChatAgent:
    return ChatAgent(
        llm=llm or RecordingLLM(),
        embedder=embedder or SpyEmbedder(),
        settings=settings_with(**settings_overrides),
        search=search or StubSearch(),
    )
