"""Mutants for the Platform Admin layer: the is_platform_admin gate, view-as-brand's separate
org-context resolver, the deactivated-brand visibility rule, and the one header that must never
widen a non-admin's access.
"""

M: list = []


def add(mid, file, old, new, args):
    M.append((mid, file, old, new, args))


DEPS = "app/api/deps.py"
ORG_SERVICE = "app/services/organizations/service.py"
APPROVALS = "app/services/operations/approvals.py"

PLATFORM_API_TESTS = ["tests/platform/test_api.py"]
PLATFORM_SERVICE_TESTS = ["tests/platform/test_service.py"]
ACTIONS_TESTS = ["tests/operations/test_actions.py"]
# org_from_path backs the existing /api/organizations/{organization_id}/... routes too, so a
# regression there is exactly what the generic cross-tenant matrix already checks for every
# non-admin user; it is the test that actually catches this one, not anything of my own.
ORG_FROM_PATH_TESTS = ["tests/test_tenant_api_matrix.py", "tests/platform/test_api.py"]

# ---- the platform-admin gate itself --------------------------------------------------------------
add(
    "plat-01-platform-admin-gate-removed",
    DEPS,
    "    if not user.is_platform_admin:\n"
    '        raise HTTPException(status.HTTP_403_FORBIDDEN, "Platform admin access required")',
    "    if False:\n"
    '        raise HTTPException(status.HTTP_403_FORBIDDEN, "Platform admin access required")',
    PLATFORM_API_TESTS,
)

# ---- view-as-brand must never activate for a non-admin -------------------------------------------
add(
    "plat-02-view-as-brand-header-works-for-anyone",
    DEPS,
    "    if user.is_platform_admin and x_platform_view_organization_id is not None:",
    "    if x_platform_view_organization_id is not None:",
    PLATFORM_API_TESTS,
)

# ---- org_from_path must also gate the admin resolver on is_platform_admin -------------------------
add(
    "plat-03-org-from-path-always-uses-admin-resolver",
    DEPS,
    "    if user.is_platform_admin:\n"
    "        ctx = resolve_platform_admin_org_context(db, user, organization_id)\n"
    "    else:\n"
    "        ctx = resolve_org_context(db, user, organization_id)",
    "    ctx = resolve_platform_admin_org_context(db, user, organization_id)",
    ORG_FROM_PATH_TESTS,
)

# ---- resolve_platform_admin_org_context must still require the org to exist -----------------------
add(
    "plat-04-admin-resolver-ignores-missing-org",
    ORG_SERVICE,
    "    org = db.get(Organization, organization_id)\n"
    "    if org is None:\n"
    "        return None\n"
    "    return OrgContext(user=admin, organization=org, role=OrganizationRole.OWNER)",
    "    org = db.get(Organization, organization_id)\n"
    "    return OrgContext(\n"
    "        user=admin,\n"
    "        organization=org if org is not None else Organization(name='x', slug='x'),\n"
    "        role=OrganizationRole.OWNER,\n"
    "    )",
    PLATFORM_SERVICE_TESTS,
)

# ---- a deactivated brand must stay invisible to resolve_org_context (its own members too) ---------
add(
    "plat-05-deactivated-brand-still-reachable-by-its-own-member",
    ORG_SERVICE,
    "        .where(OrganizationMember.user_id == user.id, Organization.is_active.is_(True))",
    "        .where(OrganizationMember.user_id == user.id)",
    PLATFORM_SERVICE_TESTS,
)

# ---- concurrent approval: the atomic rowcount guard must not be bypassed --------------------------
add(
    "plat-06-concurrent-approval-rowcount-guard-removed",
    APPROVALS,
    "            approved_at=now or datetime.now(UTC),\n"
    "        )\n"
    "    )\n"
    "    if result.rowcount != 1:  # type: ignore[attr-defined]\n"
    "        db.refresh(approval)\n"
    '        raise ApprovalNotPending(f"approval is {approval.status.value}, not PENDING")',
    "            approved_at=now or datetime.now(UTC),\n"
    "        )\n"
    "    )\n"
    "    if False:\n"
    "        db.refresh(approval)\n"
    '        raise ApprovalNotPending(f"approval is {approval.status.value}, not PENDING")',
    ACTIONS_TESTS,
)
