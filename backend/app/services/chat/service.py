"""Website chat: widget lookup, conversations, messages.

Public requests never name an organization. They carry a public key; the key resolves to exactly
one widget, and every later query is scoped to that widget's organization. A conversation is only
reachable with its own secret token, so knowing a conversation id (or another organization's public
key) is not enough to read or write it.
"""

import hashlib
import hmac
import logging
import re
import secrets
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.logging import log_event
from app.db.models import (
    AIHandoffState,
    ChatWidget,
    Conversation,
    ConversationChannel,
    ConversationStatus,
    Message,
    MessageRole,
    Organization,
    SenderType,
    SupportChannel,
)
from app.db.models.chat import PUBLIC_KEY_PREFIX
from app.db.scoping import scoped_select
from app.services.ai.agent import AssistantUnavailable, ChatAgent
from app.services.ai.provider import ChatMessage
from app.services.support import conversations as support_service

logger = logging.getLogger(__name__)

DEFAULT_WELCOME = "Hi! How can I help you today?"
LOCK_SECONDS = 90  # upper bound on one reply; a crashed request can never wedge a conversation
HISTORY_FETCH_FACTOR = 3  # fetch a few extra rows so collapsing unanswered messages still fills it
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_BLANK_LINES_RE = re.compile(r"\n{3,}")
_PUBLIC_KEY_RE = re.compile(r"^pk_[A-Za-z0-9_-]{16,61}$")


class ChatNotAvailable(Exception):
    """Unknown, malformed or disabled public key. Deliberately indistinguishable."""


class OriginNotAllowed(Exception):
    pass


class ConversationNotFound(Exception):
    """No such conversation for this widget and token. Deliberately indistinguishable."""


class ConversationBusy(Exception):
    pass


class ConversationClosed(Exception):
    pass


class ConversationLimitReached(Exception):
    pass


class InvalidMessage(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _utcnow() -> datetime:
    return datetime.now(UTC)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def clean_user_message(text: str, max_chars: int) -> str:
    """Plain text only: drop control characters, normalize blank lines, trim."""
    text = _CONTROL_RE.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))
    text = _BLANK_LINES_RE.sub("\n\n", text).strip()
    if not text:
        raise InvalidMessage("Please enter a message.")
    if len(text) > max_chars:
        raise InvalidMessage(f"Messages can be at most {max_chars} characters long.")
    return text


# --- widget configuration (dashboard side) ------------------------------


def get_or_create_widget(db: Session, organization_id: uuid.UUID) -> ChatWidget:
    widget = db.scalar(scoped_widget(organization_id))
    if widget is not None:
        return widget
    widget = ChatWidget(organization_id=organization_id, allowed_origins=[])
    db.add(widget)
    try:
        db.commit()
    except IntegrityError:  # a concurrent request created it first
        db.rollback()
        return db.scalars(scoped_widget(organization_id)).one()
    db.refresh(widget)
    return widget


def scoped_widget(organization_id: uuid.UUID):
    return select(ChatWidget).where(ChatWidget.organization_id == organization_id)


def update_widget(
    db: Session,
    widget: ChatWidget,
    *,
    enabled: bool | None,
    welcome_message: str | None,
    allowed_origins: list[str] | None,
    clear_welcome: bool = False,
) -> ChatWidget:
    if enabled is not None:
        widget.enabled = enabled
    if clear_welcome:
        widget.welcome_message = None
    elif welcome_message is not None:
        widget.welcome_message = welcome_message
    if allowed_origins is not None:
        widget.allowed_origins = allowed_origins
    db.commit()
    db.refresh(widget)
    return widget


def effective_welcome(widget: ChatWidget) -> str:
    return widget.welcome_message or DEFAULT_WELCOME


# --- public side ------------------------------------------------------------


def resolve_widget(db: Session, public_key: str) -> ChatWidget:
    if not public_key.startswith(PUBLIC_KEY_PREFIX) or not _PUBLIC_KEY_RE.match(public_key):
        raise ChatNotAvailable
    widget = db.scalar(
        select(ChatWidget).where(ChatWidget.public_key == public_key, ChatWidget.enabled.is_(True))
    )
    if widget is None:
        raise ChatNotAvailable
    return widget


def check_origin(widget: ChatWidget, origin: str | None, trusted: Sequence[str] = ()) -> None:
    """When the organization restricted where the widget may be embedded, require a listed Origin.
    Browsers always send it on cross-origin requests, so this stops casual embedding of someone
    else's widget; it is not a security boundary (a non-browser client can set any header).

    ``trusted`` are the operator's own dashboard origins (CORS_ORIGINS). They always pass, so an
    owner who lists only their production site can still preview the widget in the dashboard."""
    allowed = widget.allowed_origins or []
    if not allowed:
        return
    normalized = origin.rstrip("/").lower() if origin else None
    if normalized is None:
        raise OriginNotAllowed
    if normalized not in allowed and normalized not in {t.rstrip("/").lower() for t in trusted}:
        raise OriginNotAllowed


def create_conversation(
    db: Session, widget: ChatWidget, visitor_id: str | None
) -> tuple[Conversation, str]:
    token = secrets.token_urlsafe(32)
    conversation = Conversation(
        organization_id=widget.organization_id,
        channel=ConversationChannel.WEBSITE,
        visitor_id=visitor_id,
        status=ConversationStatus.OPEN,
        token_hash=hash_token(token),
    )
    db.add(conversation)
    db.commit()
    db.refresh(conversation)
    log_event(
        logger,
        "chat.conversation_created",
        conversation=conversation.id,
        organization=widget.organization_id,
        channel=conversation.channel,
    )
    return conversation, token


def get_conversation(
    db: Session, widget: ChatWidget, conversation_id: uuid.UUID, token: str | None
) -> Conversation:
    """The conversation, only if it belongs to this widget's organization AND the token matches."""
    conversation = db.scalar(
        scoped_select(Conversation, widget.organization_id).where(
            Conversation.id == conversation_id
        )
    )
    supplied = hash_token(token) if token else ""
    expected = (
        conversation.token_hash if conversation is not None else hash_token("no-conversation")
    )
    # compare_digest on every path, so timing doesn't reveal whether the conversation exists
    if not hmac.compare_digest(expected, supplied) or conversation is None:
        raise ConversationNotFound
    return conversation


def list_messages(db: Session, conversation: Conversation) -> list[Message]:
    return list(
        db.scalars(
            scoped_select(Message, conversation.organization_id)
            .where(Message.conversation_id == conversation.id)
            .order_by(Message.position)
        )
    )


def recent_history(db: Session, conversation: Conversation, limit: int) -> list[ChatMessage]:
    """The newest ``limit``-ish USER/ASSISTANT messages, oldest first. One indexed query."""
    if limit <= 0:
        return []
    rows = db.scalars(
        scoped_select(Message, conversation.organization_id)
        .where(
            Message.conversation_id == conversation.id,
            Message.role.in_([MessageRole.USER, MessageRole.ASSISTANT]),
        )
        .order_by(Message.position.desc())
        .limit(limit * HISTORY_FETCH_FACTOR)
    )
    return [
        ChatMessage("user" if m.role == MessageRole.USER else "assistant", m.content)
        for m in reversed(list(rows))
    ]


def _acquire(db: Session, conversation: Conversation) -> bool:
    now = _utcnow()
    result = db.execute(
        update(Conversation)
        .where(
            Conversation.id == conversation.id,
            Conversation.organization_id == conversation.organization_id,
            or_(Conversation.locked_until.is_(None), Conversation.locked_until < now),
        )
        .values(locked_until=now + timedelta(seconds=LOCK_SECONDS))
        .execution_options(synchronize_session=False)
    )
    db.commit()
    return result.rowcount == 1  # type: ignore[attr-defined]


def _release(db: Session, conversation: Conversation) -> None:
    db.rollback()  # leave a clean transaction even if the turn failed midway
    db.execute(
        update(Conversation)
        .where(
            Conversation.id == conversation.id,
            Conversation.organization_id == conversation.organization_id,
        )
        .values(locked_until=None)
        .execution_options(synchronize_session=False)
    )
    db.commit()


def _add_message(
    db: Session,
    conversation: Conversation,
    role: MessageRole,
    content: str,
    meta=None,
    *,
    sender_type: SenderType | None = None,
    sender_user_id=None,
) -> Message:
    """Append a message. Only called while holding the conversation lock, so positions are safe."""
    db.refresh(conversation)
    conversation.message_count += 1
    conversation.updated_at = _utcnow()
    message = Message(
        organization_id=conversation.organization_id,
        conversation_id=conversation.id,
        position=conversation.message_count,
        role=role,
        content=content,
        meta=meta,
        sender_type=sender_type,
        sender_user_id=sender_user_id,
    )
    db.add(message)
    db.commit()
    return message


def _ai_state_for(db: Session, conversation: Conversation) -> AIHandoffState:
    """Phase 9: website has no prior per-channel handoff concept, so the unified
    ``SupportConversation`` row is authoritative here (see
    ``app/services/support/conversations.py`` module docstring). No row yet (before the first
    sync) means nothing has happened yet, i.e. AI_ACTIVE."""
    row = db.scalar(
        select(support_service.SupportConversation.ai_state).where(
            support_service.SupportConversation.organization_id == conversation.organization_id,
            support_service.SupportConversation.channel == SupportChannel.WEBSITE,
            support_service.SupportConversation.channel_conversation_id == conversation.id,
        )
    )
    return row or AIHandoffState.AI_ACTIVE


def send_message(
    db: Session,
    *,
    widget: ChatWidget,
    conversation: Conversation,
    content: str,
    agent: ChatAgent,
    settings: Settings,
) -> Message | None:
    """One full turn: store the customer's message, produce a grounded reply, store it.

    Returns ``None`` (never a fake reply) if a human has taken over this conversation -- the
    customer's message is still stored, but the AI never auto-replies while a human is active
    (brief section 7). Raises ConversationClosed/ConversationLimitReached/ConversationBusy before
    doing any work, and AssistantUnavailable if the reply could not be produced (the customer's
    message stays stored either way).
    """
    org_id = widget.organization_id
    if conversation.status != ConversationStatus.OPEN:
        raise ConversationClosed
    if conversation.message_count >= settings.chat_max_messages_per_conversation:
        raise ConversationLimitReached
    if not _acquire(db, conversation):
        raise ConversationBusy
    try:
        history = recent_history(db, conversation, settings.chat_history_limit)
        business_name = db.scalar(select(Organization.name).where(Organization.id == org_id)) or ""
        _add_message(db, conversation, MessageRole.USER, content, sender_type=SenderType.CUSTOMER)
        log_event(
            logger,
            "chat.message_received",
            conversation=conversation.id,
            organization=org_id,
            chars=len(content),
            history=len(history),
        )
        ai_state = _ai_state_for(db, conversation)
        support_service.sync_conversation(
            db,
            organization_id=org_id,
            channel=SupportChannel.WEBSITE,
            channel_conversation_id=conversation.id,
            ai_state=ai_state,
            direction="inbound",
            sender_type=SenderType.CUSTOMER,
            event_type="website_message",
            customer_id=conversation.customer_id,
            message_preview=content[:300],
        )
        db.commit()
        from app.services.automation import ingest_event
        from app.workers.queue import get_job_queue

        is_first_message = conversation.message_count == 1
        ingest_event(
            db,
            get_job_queue(),
            organization_id=org_id,
            event_type=(
                "website_conversation_started"
                if is_first_message
                else "conversation_message_received"
            ),
            source="website",
            source_id=str(conversation.id),
            dedupe_key=f"website:message:{conversation.id}:{conversation.message_count}",
            payload={
                "event": {"source": "website"},
                "customer": {
                    "exists": conversation.customer_id is not None,
                    "id": str(conversation.customer_id) if conversation.customer_id else None,
                },
                "website": {"conversation_id": str(conversation.id)},
            },
        )

        if ai_state != AIHandoffState.AI_ACTIVE:
            return None

        # No database transaction stays open while the embedding and LLM calls run.
        db.rollback()
        try:
            reply = agent.respond(
                db,
                organization_id=org_id,
                business_name=business_name,
                question=content,
                history=history,
                conversation_id=conversation.id,
            )
        finally:
            db.rollback()
        assistant = _add_message(
            db,
            conversation,
            MessageRole.ASSISTANT,
            reply.content,
            reply.audit(),
            sender_type=SenderType.AI,
        )
        support_service.sync_conversation(
            db,
            organization_id=org_id,
            channel=SupportChannel.WEBSITE,
            channel_conversation_id=conversation.id,
            ai_state=ai_state,
            direction="outbound",
            sender_type=SenderType.AI,
            event_type="ai_reply",
            customer_id=conversation.customer_id,
        )
        db.commit()
        log_event(
            logger,
            "chat.response_stored",
            conversation=conversation.id,
            organization=org_id,
            grounded=reply.grounded,
            chars=len(reply.content),
            latency_ms=reply.latency_ms,
        )
        return assistant
    except AssistantUnavailable as exc:
        log_event(
            logger,
            "chat.reply_failed",
            level=logging.WARNING,
            conversation=conversation.id,
            organization=org_id,
            reason=exc.reason,
        )
        raise
    finally:
        _release(db, conversation)
