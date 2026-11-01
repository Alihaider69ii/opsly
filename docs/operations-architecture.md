# Operations Architecture (Phase 10)

## Philosophy

```
Observe -> Understand -> Prioritize -> Recommend -> Automate -> Escalate
```

Structured business data + deterministic rules are the system of record. AI is the
recommendation/explanation layer only. No detector, SLA calculation, priority score, or policy
decision in this phase depends on an LLM call -- every one is plain SQL + arithmetic in
`backend/app/services/operations/`. The one place an LLM is called (`ai.py`) only narrates
context that was already fetched deterministically; it cannot cause a mutation (see
`operations-ai.md`).

## Module map

```
app/db/models/operations.py        Shipment, ReturnCase, OperationalSLA/Issue/Task/Approval/
                                    Action/AuditEvent + their enums
app/services/operations/
  priorities.py     deterministic priority scoring (URGENT/HIGH/NORMAL/LOW), explainable
  sla.py            SLA catalog + evaluator; IssueCategory -> SLACategory mapping
  detectors.py      every deterministic detector; DETECTOR_REQUIREMENTS declares each one's
                    required source columns (brief section 4)
  issues.py         OperationalIssue lifecycle (create/assign/resolve/reopen/dismiss,
                    upsert_from_detection with reopen-on-recurrence, apply_sla)
  tasks.py          OperationalTask lifecycle; claim() is the atomic UPDATE...WHERE race guard
  approvals.py      OperationalApproval state machine (used only through actions.py)
  actions.py        the safe action framework: ACTION_POLICY map, request/approve/reject/execute,
                    idempotency via (org, action_type, resource_type, resource_id, idempotency_key)
  audit.py          append-only OperationalAuditEvent ledger
  bridge.py         Support -> Operations bridge; pushes brief-section-23 events onto the
                    existing Phase 9 SupportTimelineEvent (never a second timeline table)
  analytics.py      every brief-section-27 aggregate, real SQL, no faked numbers
  repository.py     read-only tenant-scoped queries backing the API
  ai.py             Operations AI -- see operations-ai.md
  service.py        run_detection_sweep: detectors -> issues.upsert_from_detection ->
                    automation.ingest_event (for the 7 named triggers) -> audit
app/api/operations/router.py       /api/operations/* (brief section 28), CurrentOrg/ManagerOrg
backend/tools/seed_operations_demo.py   brief section 30 demo seed, idempotent, synthetic ids
```

## What is new data vs. reused data

Reused verbatim (no new columns, no shadow copy): `Order`, `OrderItem`, `Product`,
`ProductVariant`, `Customer` (Phase 4/9). Payment signals come straight from
`Order.financial_status`; inventory velocity comes from real `OrderItem` rows joined to `Order`
over a trailing window -- there is no `InventorySnapshot` table because nothing in the current
sync populates point-in-time snapshots, and inventing one would mean faking history.

New, and why each is the smallest table that could work:

- `shipments` -- Shopify's own sync has no carrier/tracking/NDR/RTO fields today (see "Known
  limitations" below). One row per order, written by the demo seed and ready for a future
  courier-aggregator connector.
- `return_cases` -- covers RETURN/EXCHANGE/REFUND_ONLY workflow state Shopify's `financial_status`
  alone cannot express (pickup failure, exchange-pending, etc.).
- `operational_slas`, `operational_issues`, `operational_tasks`, `operational_approvals`,
  `operational_actions`, `operational_audit_events` -- the operations-intelligence layer itself;
  none of these exist anywhere else in the schema.

Every new table carries `organization_id` and every child FK is the composite
`(id, organization_id)` convention already used throughout the codebase (`app/db/scoping.py`).

## Detection -> Issue -> Task -> Automation flow

```
detectors.run_all(db, org, now)                 # pure queries, returns Detection objects
  -> issues.upsert_from_detection(db, detection) # idempotent on dedupe_key; reopens a resolved
                                                  # issue if the same root cause recurs
     -> issues.apply_sla(...)                    # due_at / breached_at, auto-seeds the SLA
                                                  # catalog on first use so ordering never matters
  -> service._automation_event_type(detector_code)
     -> automation.ingest_event(...)             # only for the 7 triggers brief section 26 names
                                                  # (order_needs_attention, shipment_delayed,
                                                  # ndr_detected, rto_detected, inventory_low,
                                                  # refund_pending, sla_breached)
  -> audit.record(...)                           # ISSUE_CREATED / SLA_BREACHED
```

`service.run_detection_sweep` is exposed as `POST /api/operations/sweep` and is meant to also run
periodically from the worker (brief doesn't mandate a schedule; Phase 10 wires the manual trigger
and the demo seed calls it once after seeding -- a cron/periodic worker job is a natural Phase 11
follow-up, intentionally not built now per the brief's scope-control section).

## Safe action framework

`actions.ACTION_POLICY` is the one place that decides SAFE_AUTOMATION vs. REQUIRES_APPROVAL vs.
FORBIDDEN, resolved once at `request_action` time. Unlisted action types are FORBIDDEN by
construction (no implicit allow). `execute_action` only runs when `status == APPROVED`; a
FORBIDDEN action is marked FAILED immediately and never reaches an executor. Every action is
idempotent on `(organization_id, action_type, resource_type, resource_id, idempotency_key)` --
see the automated mutation tests `op-05` through `op-07`, `op-10` for exactly which invariants are
checked.

Destructive actions (`refund`, `cancel_order`, `modify_order`, `change_customer_address`,
`customer_compensation`) have no live external system to call in Phase 10 (no Shopify
order-mutation API is wired up -- see "Known limitations"). Their executor records the
request/approval/execution in BizFlow's own ledger and returns a result that says so explicitly
(`actions._record_only`) rather than pretending a real refund or cancellation happened.
`inventory_adjustment` and `refund` (when the resource is a `return_case`) do perform a real local
mutation (`ProductVariant.inventory_quantity`, `ReturnCase.status`).

## Support -> Operations bridge

`bridge.create_issue_and_task_from_support` builds exactly the chain brief section 16 asks for:
`SupportConversation -> OperationalIssue -> OperationalTask -> Operations Team`, reusing the
conversation's own `customer_id` (never re-matching identity) and pushing
`operational_issue_created`/`operational_task_created` onto the existing
`SupportTimelineEvent` ledger (brief section 23), so they show up in the Phase 9 customer timeline
unchanged.

## Known limitations (documented, not hidden)

- **No live courier/NDR/RTO webhook integration.** `shipments` rows are written by the demo seed
  only; a real connector (e.g. Shiprocket/Delhivery) would write into the same table and the
  detectors/SLA/priority/AI layers above it need no change.
- **No COD / payment-gateway-method detector.** The current `Order` model has no payment method
  or gateway-transaction-id column, so `COD order` and `duplicate payment signal` from brief
  section 4 are not implemented (fabricating this from `financial_status` alone would be a guess).
- **No address/pincode issue detector.** `Order` has no shipping-address columns synced today.
- **No live Shopify order-mutation API.** `cancel_order`/`modify_order`/`change_customer_address`
  go through the full approval pipeline but execute as ledger-only records (see above).
- **CSV/Google Sheets/Excel connector (brief section 24)** -- the `DataConnector` abstraction
  (connect/disconnect/inspect_schema/preview/sync) and a CSV importer were scope-cut for this pass
  in favor of depth on the core engine; not built. Noted as `NOT YET IMPLEMENTED`, not faked.
- **No periodic worker schedule for the detection sweep** -- it runs on demand
  (`POST /api/operations/sweep`) and once from the demo seed; a cron-style periodic trigger is a
  natural next step, not built this phase.
- **`assign_task` and `send_internal_notification` automation action types** exist in the safe
  action policy map but are not exposed as `app/services/automation.py` workflow action types
  (only `create_issue` was added there) -- ambiguous target resolution for a generic workflow
  step made this a deliberate scope cut; they work when requested directly through the
  `/api/operations/actions` API.
