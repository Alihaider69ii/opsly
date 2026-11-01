"""Unified support inbox dashboard API (Phase 9): conversations across WhatsApp/Website/Email,
the customer timeline, and AI/human switching, assignment and priority actions. Reading is open
to every member; so are the support actions below (replying, taking over, assigning, changing
priority) -- these are everyday support operations, not billing/destructive actions, mirroring
how WhatsApp's own dashboard already treats conversation reads/handoff claim/resolve as
member-level (only template sync and consent writes need OWNER/ADMIN there).
"""

import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal, cast

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select

from app.api.deps import CurrentOrg, DbSession
from app.core.config import Settings, get_settings
from app.db.models import (
    Customer,
    PrioritySource,
    SupportChannel,
    SupportConversation,
    SupportPriority,
    SupportStatus,
)
from app.schemas.support import (
    ActionResult,
    AnalyticsOut,
    AssignIn,
    ConversationSummaryOut,
    CustomerSummaryOut,
    MessageOut,
    PriorityIn,
    ReplyIn,
    RequestHumanIn,
    TimelineEventOut,
)
from app.services.email.errors import EmailError
from app.services.messaging.errors import MessagingError
from app.services.support import analytics as analytics_service
from app.services.support import conversations as support_service
from app.services.support import messages as messages_service
from app.services.support import timeline as timeline_service
from app.services.support.conversations import ChannelNotConfigured, NotOrganizationMember
from app.workers.queue import JobQueue, get_job_queue

router = APIRouter(prefix="/support", tags=["support"])

AppSettings = Annotated[Settings, Depends(get_settings)]
Queue = Annotated[JobQueue, Depends(get_job_queue)]


def _conversation(
    db: DbSession, ctx: CurrentOrg, conversation_id: uuid.UUID
) -> SupportConversation:
    try:
        return support_service.get_conversation(
            db, organization_id=ctx.organization.id, conversation_id=conversation_id
        )
    except support_service.ConversationNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found") from None


def _summary(db: DbSession, conversation: SupportConversation) -> ConversationSummaryOut:
    customer = None
    if conversation.customer_id is not None:
        row = db.get(Customer, conversation.customer_id)
        if row is not None:
            customer = CustomerSummaryOut.model_validate(row)
    out = ConversationSummaryOut.model_validate(conversation)
    out.customer = customer
    return out


@router.get("/conversations", response_model=list[ConversationSummaryOut])
def list_conversations(
    ctx: CurrentOrg,
    db: DbSession,
    channel: SupportChannel | None = None,
    priority: SupportPriority | None = None,
    status_filter: SupportStatus | None = None,
    assignment: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[ConversationSummaryOut]:
    parsed_assignment: Literal["all", "me", "unassigned"] | uuid.UUID | None = None
    if assignment in ("all", "me", "unassigned"):
        parsed_assignment = cast(Literal["all", "me", "unassigned"], assignment)
    elif assignment:
        try:
            parsed_assignment = uuid.UUID(assignment)
        except ValueError:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid assignment filter"
            ) from None
    rows = support_service.list_inbox(
        db,
        organization_id=ctx.organization.id,
        channel=channel,
        priority=priority,
        status=status_filter,
        assignment=None if parsed_assignment == "all" else parsed_assignment,
        viewer_user_id=ctx.user.id,
        limit=limit,
        offset=offset,
    )
    return [_summary(db, row) for row in rows]


@router.get("/conversations/{conversation_id}", response_model=ConversationSummaryOut)
def get_conversation(
    conversation_id: uuid.UUID, ctx: CurrentOrg, db: DbSession
) -> ConversationSummaryOut:
    return _summary(db, _conversation(db, ctx, conversation_id))


@router.get("/conversations/{conversation_id}/messages", response_model=list[MessageOut])
def list_messages(conversation_id: uuid.UUID, ctx: CurrentOrg, db: DbSession) -> list[MessageOut]:
    conversation = _conversation(db, ctx, conversation_id)
    return [MessageOut(**vars(m)) for m in messages_service.list_messages(db, conversation)]


@router.get("/conversations/{conversation_id}/timeline", response_model=list[TimelineEventOut])
def conversation_timeline(
    conversation_id: uuid.UUID, ctx: CurrentOrg, db: DbSession, limit: int = 50
) -> list[TimelineEventOut]:
    conversation = _conversation(db, ctx, conversation_id)
    rows = timeline_service.list_conversation_timeline(
        db,
        organization_id=ctx.organization.id,
        support_conversation_id=conversation.id,
        limit=limit,
    )
    return [TimelineEventOut.model_validate(r) for r in rows]


@router.get("/customers/{customer_id}/timeline", response_model=list[TimelineEventOut])
def customer_timeline(
    customer_id: uuid.UUID, ctx: CurrentOrg, db: DbSession, limit: int = 50
) -> list[TimelineEventOut]:
    customer = db.scalar(
        select(Customer).where(
            Customer.id == customer_id, Customer.organization_id == ctx.organization.id
        )
    )
    if customer is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Customer not found")
    rows = timeline_service.list_customer_timeline(
        db, organization_id=ctx.organization.id, customer_id=customer.id, limit=limit
    )
    return [TimelineEventOut.model_validate(r) for r in rows]


@router.post("/conversations/{conversation_id}/reply", response_model=MessageOut)
def reply(
    conversation_id: uuid.UUID,
    body: ReplyIn,
    ctx: CurrentOrg,
    db: DbSession,
    queue: Queue,
    settings: AppSettings,
) -> MessageOut:
    conversation = _conversation(db, ctx, conversation_id)
    try:
        result = support_service.send_human_reply(
            db,
            queue,
            conversation=conversation,
            by_user_id=ctx.user.id,
            content=body.content,
            settings=settings,
        )
    except NotOrganizationMember:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Not a member of this organization"
        ) from None
    except ChannelNotConfigured as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from None
    except (MessagingError, EmailError) as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"could not send reply ({exc.code})"
        ) from None
    unified = [
        m
        for m in messages_service.list_messages(db, conversation, limit=1_000_000)
        if m.id == result.id
    ]
    if unified:
        return MessageOut(**vars(unified[0]))
    # Website reply rows share the Message model directly; build the shape without a re-query.
    result_status = getattr(result, "status", None)
    return MessageOut(
        id=result.id,
        direction="OUTBOUND",
        sender_type=getattr(result, "sender_type", None),
        sender_user_id=getattr(result, "sender_user_id", None),
        content=getattr(result, "content", None) or getattr(result, "body_text", None),
        status=result_status.value if result_status else "SENT",
        ai=None,
        policy=None,
        error_code=getattr(result, "error_code", None),
        created_at=result.created_at,
    )


def _action(db: DbSession, conversation: SupportConversation, ok: bool) -> ActionResult:
    return ActionResult(ok=ok, conversation=_summary(db, conversation))


@router.post("/conversations/{conversation_id}/take-over", response_model=ActionResult)
def take_over(
    conversation_id: uuid.UUID, ctx: CurrentOrg, db: DbSession, queue: Queue
) -> ActionResult:
    conversation = _conversation(db, ctx, conversation_id)
    try:
        ok = support_service.take_over(
            db, queue, conversation=conversation, by_user_id=ctx.user.id, now=datetime.now(UTC)
        )
    except NotOrganizationMember:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Not a member of this organization"
        ) from None
    db.commit()
    return _action(db, conversation, ok)


@router.post("/conversations/{conversation_id}/return-to-ai", response_model=ActionResult)
def return_to_ai(
    conversation_id: uuid.UUID, ctx: CurrentOrg, db: DbSession, queue: Queue
) -> ActionResult:
    conversation = _conversation(db, ctx, conversation_id)
    ok = support_service.return_to_ai(db, queue, conversation=conversation, now=datetime.now(UTC))
    db.commit()
    return _action(db, conversation, ok)


@router.post("/conversations/{conversation_id}/request-human", response_model=ActionResult)
def request_human(
    conversation_id: uuid.UUID, body: RequestHumanIn, ctx: CurrentOrg, db: DbSession, queue: Queue
) -> ActionResult:
    conversation = _conversation(db, ctx, conversation_id)
    ok = support_service.request_human(
        db, queue, conversation=conversation, reason=body.reason, now=datetime.now(UTC)
    )
    db.commit()
    return _action(db, conversation, ok)


@router.post("/conversations/{conversation_id}/resolve", response_model=ActionResult)
def resolve(
    conversation_id: uuid.UUID, ctx: CurrentOrg, db: DbSession, queue: Queue
) -> ActionResult:
    conversation = _conversation(db, ctx, conversation_id)
    support_service.resolve_conversation(
        db, queue, conversation=conversation, by_user_id=ctx.user.id, now=datetime.now(UTC)
    )
    db.commit()
    return _action(db, conversation, True)


@router.post("/conversations/{conversation_id}/reopen", response_model=ActionResult)
def reopen(conversation_id: uuid.UUID, ctx: CurrentOrg, db: DbSession) -> ActionResult:
    conversation = _conversation(db, ctx, conversation_id)
    ok = support_service.reopen_conversation(
        db, conversation=conversation, by_user_id=ctx.user.id, now=datetime.now(UTC)
    )
    db.commit()
    return _action(db, conversation, ok)


@router.post("/conversations/{conversation_id}/claim", response_model=ActionResult)
def claim(conversation_id: uuid.UUID, ctx: CurrentOrg, db: DbSession) -> ActionResult:
    conversation = _conversation(db, ctx, conversation_id)
    try:
        ok = support_service.claim(db, conversation=conversation, agent_user_id=ctx.user.id)
    except NotOrganizationMember:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Not a member of this organization"
        ) from None
    db.commit()
    return _action(db, conversation, ok)


@router.post("/conversations/{conversation_id}/assign", response_model=ActionResult)
def assign(
    conversation_id: uuid.UUID, body: AssignIn, ctx: CurrentOrg, db: DbSession
) -> ActionResult:
    conversation = _conversation(db, ctx, conversation_id)
    try:
        support_service.assign(
            db, conversation=conversation, agent_user_id=body.agent_user_id, assigned_by=ctx.user.id
        )
    except NotOrganizationMember:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "Agent is not a member of this organization"
        ) from None
    db.commit()
    return _action(db, conversation, True)


@router.post("/conversations/{conversation_id}/release", response_model=ActionResult)
def release(conversation_id: uuid.UUID, ctx: CurrentOrg, db: DbSession) -> ActionResult:
    conversation = _conversation(db, ctx, conversation_id)
    support_service.release(db, conversation=conversation)
    db.commit()
    return _action(db, conversation, True)


@router.post("/conversations/{conversation_id}/priority", response_model=ActionResult)
def set_priority(
    conversation_id: uuid.UUID, body: PriorityIn, ctx: CurrentOrg, db: DbSession
) -> ActionResult:
    conversation = _conversation(db, ctx, conversation_id)
    ok = support_service.set_priority(
        db,
        conversation=conversation,
        priority=body.priority,
        source=PrioritySource.HUMAN,
        reason=body.reason,
        changed_by=ctx.user.id,
    )
    db.commit()
    return _action(db, conversation, ok)


@router.get("/analytics", response_model=AnalyticsOut)
def analytics(ctx: CurrentOrg, db: DbSession) -> AnalyticsOut:
    result = analytics_service.compute(db, organization_id=ctx.organization.id)
    return AnalyticsOut(**result.__dict__)
