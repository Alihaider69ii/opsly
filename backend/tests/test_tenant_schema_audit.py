"""The database schema itself is the last line of tenant isolation.

Every table is either global (a user account) or belongs to exactly one organization, and a row
can only ever point at rows of its own organization. A new table that is not classified here, or a
tenant table that is missing the column or the composite reference, fails this audit.
"""

import pytest
from sqlalchemy import Table

from app.db import models  # noqa: F401  (registers every table)
from app.db.base import Base

GLOBAL_TABLES = {
    "users": "an account exists independently of the businesses it belongs to",
    "organizations": "the tenant root: every other row hangs off one of these",
}

# Tenant-owned tables whose organization_id is deliberately not enforced by a foreign key.
NO_ORG_FK = {
    "shopify_webhook_events": (
        "an authentic webhook for a shop that is not (or no longer) connected has no organization "
        "yet; the row is kept as an audit record of what was received and is purged on disconnect"
    ),
    "whatsapp_webhook_events": (
        "a webhook delivery for a phone_number_id that matches no (or no longer connected) "
        "connection is still an authentic Meta delivery worth a ledger row for dedupe/audit, "
        "even though it never resolves to an organization"
    ),
    "email_webhook_events": (
        "an inbound email to an address that matches no (or no longer connected) connection, or a "
        "status event for a provider_message_id BizFlow cannot yet resolve, is still worth a "
        "ledger row for dedupe/audit, even though it never resolves to an organization"
    ),
    "platform_audit_events": (
        "some platform-level events (e.g. PLATFORM_ADMIN_LOGIN) are not about any one brand, and "
        "an event that IS about a brand must still survive that brand being deleted -- the audit "
        "trail is append-only and outlives the organization it references, so this FK is "
        "ondelete=SET NULL rather than CASCADE (it does have a real FK; only the cascade shape "
        "differs from the usual tenant-owned-row convention)"
    ),
}
# Tenant-owned tables where organization_id may be NULL, with the reason.
NULLABLE_ORG = {
    "shopify_webhook_events": NO_ORG_FK["shopify_webhook_events"],
    "whatsapp_webhook_events": NO_ORG_FK["whatsapp_webhook_events"],
    "email_webhook_events": NO_ORG_FK["email_webhook_events"],
    "platform_audit_events": NO_ORG_FK["platform_audit_events"],
}


def tenant_tables() -> list[Table]:
    return [t for name, t in sorted(Base.metadata.tables.items()) if name not in GLOBAL_TABLES]


def org_fk(table: Table):
    column = table.c.organization_id
    return [fk for fk in column.foreign_keys if fk.column.table.name == "organizations"]


def test_every_table_is_classified_as_global_or_tenant_owned():
    names = set(Base.metadata.tables)
    assert set(GLOBAL_TABLES) <= names
    for table in tenant_tables():
        assert "organization_id" in table.c, f"{table.name} has no organization_id"


@pytest.mark.parametrize("table", tenant_tables(), ids=lambda t: t.name)
def test_a_tenant_table_belongs_to_exactly_one_organization_that_cannot_be_null(table):
    column = table.c.organization_id
    if table.name in NULLABLE_ORG:
        assert column.nullable
    else:
        assert not column.nullable, f"{table.name}.organization_id must be NOT NULL"


@pytest.mark.parametrize("table", tenant_tables(), ids=lambda t: t.name)
def test_deleting_an_organization_removes_every_row_it_owns(table):
    if table.name in NO_ORG_FK:
        pytest.skip(NO_ORG_FK[table.name])
    fks = org_fk(table)
    assert fks, f"{table.name}.organization_id has no foreign key to organizations"
    assert all(fk.ondelete == "CASCADE" for fk in fks), f"{table.name} would orphan rows"


def unsafe_references(table: Table) -> list[str]:
    """References that would let a row point at another organization's row."""
    problems = []
    for constraint in table.foreign_key_constraints:
        target = constraint.referred_table
        if target.name in GLOBAL_TABLES:
            continue
        local = {c.name for c in constraint.columns}
        remote = {e.column.name for e in constraint.elements}
        if "organization_id" not in local or "organization_id" not in remote:
            problems.append(f"{table.name}{sorted(local)} -> {target.name}")
    return problems


@pytest.mark.parametrize("table", tenant_tables(), ids=lambda t: t.name)
def test_a_reference_to_another_tenant_table_must_carry_the_organization_too(table):
    """A plain ``product_id -> products.id`` would let a row point at another tenant's product; the
    composite ``(product_id, organization_id) -> (products.id, products.organization_id)`` cannot."""
    assert unsafe_references(table) == []


def test_the_audit_itself_detects_an_unsafe_reference():
    from sqlalchemy import Column, ForeignKey, ForeignKeyConstraint, MetaData, Uuid
    from sqlalchemy import Table as T

    meta = MetaData()
    T("organizations", meta, Column("id", Uuid, primary_key=True))
    parent = T(
        "things",
        meta,
        Column("id", Uuid, primary_key=True),
        Column("organization_id", Uuid, ForeignKey("organizations.id")),
    )
    unsafe = T(
        "bad_children",
        meta,
        Column("id", Uuid, primary_key=True),
        Column("organization_id", Uuid, ForeignKey("organizations.id")),
        Column("thing_id", Uuid, ForeignKey("things.id")),  # plain reference: crosses tenants
    )
    safe = T(
        "good_children",
        meta,
        Column("id", Uuid, primary_key=True),
        Column("organization_id", Uuid, ForeignKey("organizations.id")),
        Column("thing_id", Uuid),
        ForeignKeyConstraint(
            ["thing_id", "organization_id"], ["things.id", "things.organization_id"]
        ),
    )
    assert unsafe_references(parent) == []
    assert unsafe_references(unsafe) == ["bad_children['thing_id'] -> things"]
    assert unsafe_references(safe) == []


@pytest.mark.parametrize("table", tenant_tables(), ids=lambda t: t.name)
def test_the_composite_reference_target_is_guaranteed_unique(table):
    """A composite foreign key is only valid (and only meaningful) against a unique key."""
    for constraint in table.foreign_key_constraints:
        target = constraint.referred_table
        if target.name in GLOBAL_TABLES:
            continue
        wanted = {e.column.name for e in constraint.elements}
        keys = [
            {c.name for c in u.columns}
            for u in target.constraints
            if u.__class__.__name__ in {"UniqueConstraint", "PrimaryKeyConstraint"}
        ]
        keys += [{c.name for c in i.columns} for i in target.indexes if i.unique]
        assert wanted in keys, f"{target.name} has no unique key on {sorted(wanted)}"


@pytest.mark.parametrize("table", tenant_tables(), ids=lambda t: t.name)
def test_every_tenant_table_can_be_queried_by_organization_without_a_full_scan(table):
    indexed_first = {list(i.columns)[0].name for i in table.indexes if len(i.columns) > 0} | {
        list(c.columns)[0].name
        for c in table.constraints
        if c.__class__.__name__ == "UniqueConstraint" and len(c.columns) > 0
    }
    column = table.c.organization_id
    assert (
        column.index
        or "organization_id" in indexed_first
        or any(
            # a foreign-key column that is the leading column of the primary key
            pk.name == "organization_id"
            for pk in list(table.primary_key.columns)[:1]
        )
    ), f"{table.name}.organization_id has no leading index"
