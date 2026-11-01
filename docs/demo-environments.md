# Demo Environments

Four synthetic brands for demos and testing, each tagged `Organization.environment = DEMO` so
demo data can never be mistaken for a real customer's (brief section 9) and each built with a real
`OWNER`/`ADMIN`/`MEMBER` team and real operational data detected by the actual Phase 10 engine --
not hand-inserted rows.

## The four brands

| Brand | Category | Slug |
|---|---|---|
| UrbanThread | Fashion / Apparel | `urbanthread` |
| GlowCart | Beauty / Skincare | `glowcart` |
| TechNest | Electronics | `technest` |
| NovaHome | Home / Lifestyle | `novahome` |

Each has three synthetic users:

```
owner@<slug>.demo      OWNER
admin@<slug>.demo      ADMIN
support@<slug>.demo    MEMBER
```

All four share the password `Demo12345!`. This is a fixed, synthetic, local/demo-only value
(`backend/tools/seed_demo_platform.py:DEMO_PASSWORD`) -- not a real credential, never used for a
`PRODUCTION`-tagged organization, and not something a startup script reads from an environment
variable (unlike the platform admin's own credentials, below), because these accounts only ever
need to exist inside a demo database an operator spun up on purpose.

## Platform admin

Separate from the four brands. Created by `backend/tools/seed_platform_admin.py`, which reads
`PLATFORM_ADMIN_EMAIL` / `PLATFORM_ADMIN_PASSWORD` from the environment (`.env`) -- never
hard-coded, never committed. Re-running the command is idempotent: it updates the existing user's
password and `is_platform_admin` flag rather than creating a duplicate. Refuses to run against a
production environment unless `--i-know-what-im-doing` is passed.

```bash
cd backend
python -m tools.seed_platform_admin                       # reads .env
python -m tools.seed_platform_admin --email founder@bizflow.test --password "..."   # override
```

## Seeding the demo brands

```bash
cd backend
python -m tools.seed_demo_platform
```

Idempotent: finds each brand by its reserved slug instead of creating a duplicate, and the
underlying `tools/seed_operations_demo.py:seed()` call (reused verbatim, once per brand) is itself
idempotent -- re-running updates existing operational issues rather than duplicating them.
Live-verified twice in a row: the first run reports N issues *created*; the second reports 0
created / N *updated*, with the organization/customer/order counts unchanged.

Each brand gets, via the real detection sweep (not inserted directly):

- 6 customers, 4 product variants (one each: out-of-stock, low-stock, dead-stock, bestseller), 9
  orders (normal/high-value/unfulfilled/duplicate), 4 shipments (one delayed, one repeated-NDR,
  two same-customer RTO), 3 return cases.
- 14 operational issues, detected by the real `services/operations/detectors.py` engine and run
  through the real priority/SLA pipeline -- proving `raw data -> detector -> issue ->
  priority/SLA -> task/action` actually works, exactly as the brief asks.

### `--reset-demo`

```bash
python -m tools.seed_demo_platform --reset-demo
```

Deletes and recreates **only** the four organizations listed above, and only when the existing
organization at that slug is still tagged `DEMO`. Live-verified: a brand manually re-tagged
`PRODUCTION` was correctly skipped (`SKIP ... refusing to touch it`) while the other three DEMO
brands were torn down and rebuilt. This is the only destructive path in the tool, and it is
narrowly scoped on purpose (brief section 48) -- it will never touch an organization outside the
four reserved slugs, and never one tagged `PRODUCTION` even if a slug collided.

## What this proves end to end

Live-tested over real HTTP against the four seeded brands (see `docs/phase-10-progress.md` for
the full transcript): platform admin dashboard reporting real aggregate counts (4 brands, 56 open
issues, 24 customers, 13 users), entering/switching/exiting brands with the correct isolated data
at each step, and a freshly-created brand owner logging in through the normal `/api/auth/login`
and seeing only their own organization.
