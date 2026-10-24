"""Shopify deletes API-created webhook subscriptions after 8 consecutive failed deliveries (about
four hours of our downtime). The auditor notices, repairs, and catches up on what was missed."""

import logging
import uuid

import pytest
from sqlalchemy import select, update

from app.db.models import (
    ShopifyConnection,
    ShopifyConnectionStatus,
    ShopifySyncState,
    WebhookRegistration,
)
from app.services.integrations.shopify import webhooks
from app.services.integrations.shopify.subscriptions import SubscriptionAuditor
from app.workers.queue import InMemoryJobQueue, ShopifySyncJob
from tests.helpers import create_org, signup_and_login
from tests.shopify.conftest import seed_connection

TOPICS = 10  # 9 resource topics + app/uninstalled


@pytest.fixture()
def org_id(client):
    return uuid.UUID(
        create_org(client, signup_and_login(client, "audit@example.com"), "Audit Co")["id"]
    )


@pytest.fixture()
def cid(session_factory, org_id, runtime, fake):
    connection = seed_connection(session_factory, org_id, runtime, fake)
    webhooks.register_webhooks(runtime, session_factory, connection)
    assert len(fake.subscriptions) == TOPICS
    return connection


@pytest.fixture()
def auditor(clock):
    return SubscriptionAuditor(3600, clock=clock.monotonic)


@pytest.fixture()
def jobs() -> InMemoryJobQueue:
    return InMemoryJobQueue()


def state(session_factory, cid) -> ShopifyConnection:
    with session_factory() as db:
        return db.get(ShopifyConnection, cid)


def test_healthy_subscriptions_cause_no_changes_and_no_sync(
    runtime, session_factory, cid, fake, auditor, jobs
):
    created_before = len(fake.subscriptions)
    result = auditor.run(runtime, session_factory, jobs)
    assert (result.checked, result.repaired, result.catch_up_syncs) == (1, 0, 0)
    assert len(fake.subscriptions) == created_before and len(jobs.jobs) == 0
    assert state(session_factory, cid).status == ShopifyConnectionStatus.CONNECTED
    assert state(session_factory, cid).webhook_status == WebhookRegistration.REGISTERED


def test_subscriptions_shopify_deleted_are_recreated_and_the_missed_period_is_caught_up(
    runtime, session_factory, cid, fake, auditor, jobs
):
    fake.subscriptions.clear()  # eight failed deliveries later, Shopify removed them all

    result = auditor.run(runtime, session_factory, jobs)

    assert (result.checked, result.repaired, result.catch_up_syncs) == (1, 1, 1)
    assert len(fake.subscriptions) == TOPICS  # live updates are back
    assert [type(j) for j in jobs.jobs] == [ShopifySyncJob] and jobs.jobs[0].connection_id == cid
    row = state(session_factory, cid)
    assert row.status == ShopifyConnectionStatus.SYNCING  # webhooks were missed: re-read everything
    assert row.webhook_status == WebhookRegistration.REGISTERED
    with session_factory() as db:
        stages = db.scalars(
            select(ShopifySyncState).where(ShopifySyncState.connection_id == cid)
        ).all()
    assert len(stages) == 3 and all(s.status.value == "PENDING" for s in stages)


def test_only_the_missing_topics_are_recreated(runtime, session_factory, cid, fake, auditor, jobs):
    kept = [s for s in fake.subscriptions if s["topic"] in ("PRODUCTS_UPDATE", "APP_UNINSTALLED")]
    fake.subscriptions[:] = kept
    auditor.run(runtime, session_factory, jobs)
    topics = [s["topic"] for s in fake.subscriptions]
    assert len(topics) == TOPICS and len(set(topics)) == TOPICS  # no duplicates of the survivors


def test_the_audit_is_throttled_per_connection(
    runtime, session_factory, cid, fake, auditor, jobs, clock
):
    auditor.run(runtime, session_factory, jobs)
    calls = len(fake.requests)
    assert auditor.run(runtime, session_factory, jobs).checked == 0  # too soon
    assert len(fake.requests) == calls  # not one request to Shopify
    clock.advance(3601)
    assert auditor.run(runtime, session_factory, jobs).checked == 1


def test_subscriptions_that_were_never_live_are_created_without_a_pointless_catch_up(
    runtime, session_factory, cid, fake, auditor, jobs
):
    """A store whose live updates were never on (for example the webhook URL was configured later)
    gets its subscriptions, but nothing was missed, so no sync is forced."""
    fake.subscriptions.clear()
    with session_factory() as db:
        db.execute(
            update(ShopifyConnection)
            .where(ShopifyConnection.id == cid)
            .values(webhook_status=WebhookRegistration.NOT_CONFIGURED)
        )
        db.commit()
    result = auditor.run(runtime, session_factory, jobs)
    assert (result.repaired, result.catch_up_syncs) == (1, 0)
    assert len(fake.subscriptions) == TOPICS
    assert state(session_factory, cid).status == ShopifyConnectionStatus.CONNECTED


def test_a_status_that_drifted_is_corrected_without_touching_shopify_data(
    runtime, session_factory, cid, fake, auditor, jobs
):
    with session_factory() as db:
        db.execute(
            update(ShopifyConnection)
            .where(ShopifyConnection.id == cid)
            .values(webhook_status=WebhookRegistration.FAILED)
        )
        db.commit()
    auditor.run(runtime, session_factory, jobs)
    assert state(session_factory, cid).webhook_status == WebhookRegistration.REGISTERED


@pytest.mark.parametrize(
    "status",
    [
        ShopifyConnectionStatus.SYNCING,  # a sync registers them itself
        ShopifyConnectionStatus.ERROR,  # cannot call Shopify until reconnected
        ShopifyConnectionStatus.DISCONNECTED,
    ],
)
def test_connections_that_are_not_idle_are_skipped(
    runtime, session_factory, cid, fake, auditor, jobs, status
):
    with session_factory() as db:
        db.execute(
            update(ShopifyConnection).where(ShopifyConnection.id == cid).values(status=status)
        )
        db.commit()
    fake.subscriptions.clear()
    calls = len(fake.requests)
    assert auditor.run(runtime, session_factory, jobs).checked == 0
    assert len(fake.requests) == calls and fake.subscriptions == []


def test_nothing_happens_when_live_updates_are_not_configured(
    runtime, session_factory, cid, fake, auditor, jobs, shopify_settings
):
    shopify_settings.shopify_webhook_base_url = ""
    fake.subscriptions.clear()
    calls = len(fake.requests)
    assert auditor.run(runtime, session_factory, jobs).checked == 0
    assert len(fake.requests) == calls


def test_a_shopify_outage_during_the_audit_is_logged_not_raised_and_changes_nothing(
    runtime, session_factory, cid, fake, auditor, jobs, caplog
):
    caplog.set_level(logging.WARNING)
    fake.http_failures = [503] * 20
    result = auditor.run(runtime, session_factory, jobs)  # does not raise
    assert result.checked == 0
    assert "webhook audit failed" in caplog.text
    assert state(session_factory, cid).status == ShopifyConnectionStatus.CONNECTED


def test_a_catch_up_never_overlaps_a_running_sync(
    runtime, session_factory, cid, fake, auditor, jobs
):
    """queue_sync uses the same atomic CONNECTED -> SYNCING flip as a merchant's click."""
    from app.services.integrations.shopify.sync import queue_sync

    assert queue_sync(session_factory, jobs, cid) is True
    assert queue_sync(session_factory, jobs, cid) is False  # already SYNCING
    assert len(jobs.jobs) == 1


def test_a_queue_outage_does_not_leave_the_connection_stuck_syncing(
    runtime, session_factory, cid, fake, auditor
):
    from app.services.integrations.shopify.sync import queue_sync

    class DownQueue:
        def enqueue(self, job):
            raise ConnectionError("redis is down")

        def dequeue(self, timeout=0.0):
            return None

        def close(self):
            pass

    assert queue_sync(session_factory, DownQueue(), cid) is True
    row = state(session_factory, cid)
    assert row.status == ShopifyConnectionStatus.CONNECTED  # usable again, stages show the failure
    with session_factory() as db:
        codes = {s.error_code for s in db.scalars(select(ShopifySyncState))}
    assert codes == {"queue_unavailable"}
