"""Public website chat API. No login: the widget identifies the business by its PUBLIC key in the
URL, and a conversation by an id plus the secret token issued when it was created.

Nothing here accepts an organization id. The key resolves to one widget (404 if unknown, malformed
or disabled, all identical), and every later query is scoped to that widget's organization.
"""

import logging
import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.api.deps import DbSession
from app.core.config import Settings, get_settings
from app.core.rate_limit import client_ip, rate_limit
from app.db.models import ChatWidget, Organization, WebsiteLead
from app.schemas.chat import (
    ChatReply,
    ConversationCreate,
    ConversationCreated,
    ConversationOut,
    LeadCapture,
    MessageCreate,
    MessageOut,
    WidgetPublicConfig,
)
from app.services.ai.agent import AssistantUnavailable, ChatAgent
from app.services.ai.factory import get_embedding_provider, get_llm_provider
from app.services.ai.provider import AIError
from app.services.chat import service
from app.workers.queue import JobQueue, get_job_queue

logger = logging.getLogger(__name__)

UNAVAILABLE = "The assistant is temporarily unavailable. Please try again in a moment."


def _ip_and_key(request: Request) -> str:
    return f"{client_ip(request)}:{request.path_params.get('public_key', '')}"


_ip_limit = rate_limit(
    "chat-ip",
    limit=lambda s: s.public_chat_rate_limit * 3,
    window=lambda s: s.public_chat_rate_window_seconds,
    key=client_ip,
    detail="Too many requests. Please slow down.",
)
_message_limit = rate_limit(
    "chat-message",
    limit=lambda s: s.public_chat_rate_limit,
    window=lambda s: s.public_chat_rate_window_seconds,
    key=_ip_and_key,
    detail="You are sending messages too quickly. Please wait a moment.",
)
_conversation_limit = rate_limit(
    "chat-conversation",
    limit=lambda s: s.public_chat_conversation_limit,
    window=lambda s: 3600,
    key=_ip_and_key,
    detail="Too many new conversations. Please try again later.",
)

router = APIRouter(
    prefix="/public/chat/{public_key}",
    tags=["public-chat"],
    dependencies=[Depends(_ip_limit)],
)

AppSettings = Annotated[Settings, Depends(get_settings)]


def widget_for_request(
    public_key: str, request: Request, db: DbSession, settings: AppSettings
) -> ChatWidget:
    try:
        widget = service.resolve_widget(db, public_key)
    except service.ChatNotAvailable:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Chat is not available") from None
    try:
        service.check_origin(widget, request.headers.get("origin"), settings.cors_origin_list)
    except service.OriginNotAllowed:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "This website is not allowed to use this chat"
        ) from None
    return widget


PublicWidget = Annotated[ChatWidget, Depends(widget_for_request)]


def get_chat_agent(settings: AppSettings) -> ChatAgent:
    """Builds the agent from configuration. A missing key or bad configuration is a controlled
    503 for visitors (details only in server logs), never a crash or a leaked message."""
    try:
        return ChatAgent(
            llm=get_llm_provider(), embedder=get_embedding_provider(), settings=settings
        )
    except AIError as exc:
        logger.warning("chat unavailable: provider not usable code=%s", exc.code)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, UNAVAILABLE) from None


Agent = Annotated[ChatAgent, Depends(get_chat_agent)]
ConversationToken = Annotated[str | None, Header(alias="X-Conversation-Token")]
Queue = Annotated[JobQueue, Depends(get_job_queue)]


def _message_out(message) -> MessageOut:
    return MessageOut(
        role="user" if message.role == "USER" else "assistant",
        content=message.content,
        created_at=message.created_at,
    )


@router.get("/config", response_model=WidgetPublicConfig)
def widget_config(widget: PublicWidget, db: DbSession) -> WidgetPublicConfig:
    name = db.scalar(
        Organization.__table__.select()
        .with_only_columns(Organization.name)
        .where(Organization.id == widget.organization_id)
    )
    return WidgetPublicConfig(
        business_name=name or "", welcome_message=service.effective_welcome(widget)
    )


@router.post(
    "/conversations",
    response_model=ConversationCreated,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(_conversation_limit)],
)
def create_conversation(
    body: ConversationCreate, widget: PublicWidget, db: DbSession
) -> ConversationCreated:
    conversation, token = service.create_conversation(db, widget, body.visitor_id)
    return ConversationCreated(conversation_id=conversation.id, token=token)


@router.post("/messages", response_model=ChatReply, dependencies=[Depends(_message_limit)])
def send_message(
    body: MessageCreate,
    widget: PublicWidget,
    db: DbSession,
    agent: Agent,
    settings: AppSettings,
    token: ConversationToken = None,
) -> ChatReply:
    try:
        conversation = service.get_conversation(db, widget, body.conversation_id, token)
        content = service.clean_user_message(body.content, settings.chat_max_message_chars)
        assistant = service.send_message(
            db,
            widget=widget,
            conversation=conversation,
            content=content,
            agent=agent,
            settings=settings,
        )
    except service.ConversationNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found") from None
    except service.InvalidMessage as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, exc.message) from None
    except service.ConversationBusy:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Please wait for the previous reply to finish."
        ) from None
    except service.ConversationClosed:
        raise HTTPException(status.HTTP_409_CONFLICT, "This conversation is closed.") from None
    except service.ConversationLimitReached:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This conversation has reached its message limit. Please start a new one.",
        ) from None
    except AssistantUnavailable:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, UNAVAILABLE) from None
    return ChatReply(conversation_id=body.conversation_id, message=_message_out(assistant))


@router.post("/leads", status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(_message_limit)])
def capture_lead(
    body: LeadCapture,
    widget: PublicWidget,
    db: DbSession,
    queue: Queue,
    token: ConversationToken = None,
) -> dict[str, str]:
    try:
        conversation = service.get_conversation(db, widget, body.conversation_id, token)
    except service.ConversationNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found") from None
    lead = db.scalar(
        select(WebsiteLead).where(WebsiteLead.conversation_id == conversation.id).with_for_update()
    )
    if lead is None:
        lead = WebsiteLead(
            organization_id=widget.organization_id,
            conversation_id=conversation.id,
            created_at=datetime.now(UTC),
        )
        db.add(lead)
    lead.name = body.name
    lead.email = body.email
    lead.phone = body.phone
    lead.marketing_consent = body.marketing_consent
    lead.consent_source = "website_chat"
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        lead = db.scalar(
            select(WebsiteLead)
            .where(
                WebsiteLead.conversation_id == conversation.id,
                WebsiteLead.organization_id == widget.organization_id,
            )
            .with_for_update()
        )
        if lead is None:
            raise
        lead.name = body.name
        lead.email = body.email
        lead.phone = body.phone
        lead.marketing_consent = body.marketing_consent
        db.commit()
    from app.services.automation import ingest_event

    ingest_event(
        db,
        queue,
        organization_id=widget.organization_id,
        event_type="website_lead_captured",
        source="website",
        source_id=str(conversation.id),
        dedupe_key=f"website-lead:{conversation.id}",
        payload={
            "customer": {
                "exists": True,
                "lead_id": str(lead.id),
                "email": bool(body.email),
                "phone": bool(body.phone),
                "marketing_consent": body.marketing_consent,
            }
        },
    )
    return {"status": "captured"}


@router.get("/conversations/{conversation_id}", response_model=ConversationOut)
def get_conversation(
    conversation_id: uuid.UUID,
    widget: PublicWidget,
    db: DbSession,
    token: ConversationToken = None,
) -> ConversationOut:
    try:
        conversation = service.get_conversation(db, widget, conversation_id, token)
    except service.ConversationNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found") from None
    messages = service.list_messages(db, conversation)
    return ConversationOut(
        conversation_id=conversation.id,
        status=conversation.status.value,  # type: ignore[arg-type]
        messages=[_message_out(m) for m in messages if m.role != "SYSTEM"],
    )
