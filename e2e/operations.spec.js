// Browser coverage for Phase 10 Operations: dashboard KPIs, issue detail/resolve/reopen, task
// create/claim/resolve, Operations AI, the Safe Action Framework's approval gate (including the
// 409-before-approval check), the platform audit trail, and cross-tenant isolation. Detectors,
// priority/SLA math and the action policy map are already covered deeply by backend unit/
// integration/mutation tests (backend/tests/operations/, backend/mutation/catalog_10.py) -- this
// suite's job is to prove the UI actually wires up to that real engine end to end, so test data is
// created directly through the operations API (the same endpoints the UI itself calls), not by
// hand-inserting rows.
const cfg = require("./lib/config");
const path = require("path");

const { BASE, API } = cfg;
const SHOTS = cfg.ARTIFACTS;
const stamp = Date.now();
const password = "correct-horse-battery";

let failed = 0;
function check(name, ok, extra = "") {
  if (!ok) failed++;
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${extra ? `   [${String(extra).slice(0, 240)}]` : ""}`);
}
const endsWith = (page, p) => new URL(page.url()).pathname === p;
const settleAt = async (page, p) => {
  try {
    await page.waitForURL((u) => new URL(u).pathname === p, { timeout: 15000 });
  } catch {}
};

async function apiToken(email) {
  const r = await fetch(`${API}/api/auth/login`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
  return (await r.json()).access_token;
}

async function api(token, method, pathname, body) {
  const r = await fetch(`${API}${pathname}`, {
    method,
    headers: { "content-type": "application/json", authorization: `Bearer ${token}` },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  const text = await r.text();
  return { status: r.status, json: text ? JSON.parse(text) : null };
}

async function signup(page, email, business) {
  await page.goto(`${BASE}/signup`);
  await page.fill("#email", email);
  await page.fill("#password", password);
  await Promise.all([page.waitForURL(/\/onboarding$/, { timeout: 20000 }), page.click("button[type=submit]")]);
  await page.fill("#name", business);
  await Promise.all([page.waitForURL(/\/dashboard$/, { timeout: 20000 }), page.click("button[type=submit]")]);
  await page.waitForSelector("h1:has-text('Welcome back')", { timeout: 15000 });
}

(async () => {
  const browser = await cfg.launchBrowser();
  const ctx = await browser.newContext({ viewport: { width: 1200, height: 900 } });
  const page = await ctx.newPage();
  const shot = (n) => page.screenshot({ caret: "initial", path: path.join(SHOTS, `operations-${n}.png`) });
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));

  const email = `ops-e2e-${stamp}@example.com`;
  await signup(page, email, "Ops E2E Brand");
  const token = await apiToken(email);

  // --- create a real issue through the real operations API (the same one the UI calls)
  const created = await api(token, "POST", "/api/operations/issues", {
    category: "ORDER",
    title: `E2E urgent issue ${stamp}`,
    priority: "URGENT",
  });
  check("seed: issue created via the operations API", created.status === 201, JSON.stringify(created.json));
  const issueId = created.json.id;

  // --- 1/2/3: dashboard loads with real KPI values and the seeded issue in the Urgent Queue
  await page.goto(`${BASE}/operations`);
  let body = await page.textContent("body");
  check("operations dashboard loads", body.includes("Open Issues"));
  check("urgent issue appears in the Urgent Queue", (await page.locator(`li[data-issue="${issueId}"]`).count()) === 1);
  await shot("01-dashboard");

  // --- 4/5: open the issue, inspect evidence
  await page.locator(`li[data-issue="${issueId}"] a`).click();
  await settleAt(page, `/operations/issues/${issueId}`);
  check("issue detail page loads", endsWith(page, `/operations/issues/${issueId}`));
  await page.waitForSelector('[data-testid="issue-evidence"]', { timeout: 15000 });
  check("evidence section is present", (await page.locator('[data-testid="issue-evidence"]').count()) === 1);
  await shot("02-issue-detail");

  // --- 6/7: resolve then reopen
  await page.click("button[data-action=resolve]");
  await page.waitForSelector('[data-testid="issue-status"]:has-text("RESOLVED")', { timeout: 15000 });
  check("issue status becomes RESOLVED", true);
  await page.click("button[data-action=reopen]");
  await page.waitForSelector('[data-testid="issue-status"]:has-text("OPEN")', { timeout: 15000 });
  check("issue status becomes OPEN again after reopen", true);

  // --- 8/9/10: create a task from the issue, claim it, resolve it
  await page.click("button[data-action=create-task]");
  await page.waitForSelector('[data-testid="issue-tasks"] li', { timeout: 15000 });
  check("a task appears under the issue after Create task", (await page.locator('[data-testid="issue-tasks"] li').count()) >= 1);

  await page.goto(`${BASE}/operations/tasks`);
  check("the created task appears on the tasks page", (await page.locator('[data-testid="task-list"] li').count()) >= 1);
  const taskRow = page.locator('[data-testid="task-list"] li[data-status="OPEN"]').first();
  await taskRow.locator("button[data-action=claim]").click();
  await page.waitForSelector('[data-testid="task-list"] li[data-status="ASSIGNED"]', { timeout: 15000 });
  check("claiming a task sets it to ASSIGNED", true);
  await page.locator('[data-testid="task-list"] li[data-status="ASSIGNED"]').first().locator("button[data-action=resolve]").click();
  await page.waitForSelector('[data-testid="task-list"] li[data-status="RESOLVED"]', { timeout: 15000 });
  check("resolving an assigned task sets it to RESOLVED", true);
  await shot("03-tasks");

  // --- 11: Operations AI gives a grounded, evidence-based answer
  await page.goto(`${BASE}/operations/ai`);
  await page.fill('[data-testid="ai-question-input"]', "What are today's biggest operational risks?");
  await page.click('[data-testid="ai-ask-button"]');
  await page.waitForSelector('[data-testid="ai-summary"]', { timeout: 15000 });
  const aiSummary = await page.textContent('[data-testid="ai-summary"]');
  check("Operations AI returns a non-empty grounded summary", !!aiSummary && aiSummary.trim().length > 0, aiSummary);
  await shot("04-ai");

  // --- 12/13/14: safe action request -> 409 before approval -> admin approval -> execute -> audit
  await page.goto(`${BASE}/operations/issues/${issueId}`);
  await page.selectOption("select", "refund");
  await page.click("button[data-action=request]");
  await page.waitForSelector('[data-testid="requested-action"]', { timeout: 15000 });
  const requestedBody = await page.textContent('[data-testid="requested-action"]');
  check("refund on an issue requires approval", requestedBody.includes("requires approval"));

  const actions = await api(token, "GET", `/api/operations/actions`);
  const pendingAction = actions.json.find((a) => a.resource_id === issueId && a.status === "PENDING");
  check("the pending action is findable via the API", !!pendingAction);
  const beforeApproval = await api(token, "POST", `/api/operations/actions/${pendingAction.id}/execute`);
  check("executing before approval is rejected with 409", beforeApproval.status === 409, beforeApproval.status);

  await page.click("button[data-action=approve]");
  await page.waitForSelector("button[data-action=execute]", { timeout: 15000 });
  check("Approve shows the Execute button", true);
  await page.click("button[data-action=execute]");
  await page.waitForSelector('[data-testid="requested-action"]:has-text("EXECUTED")', { timeout: 15000 });
  check("executing an approved action marks it EXECUTED", true);
  await shot("05-action-executed");

  // --- 15: audit trail shows the full lifecycle. The issue's own resource-scoped audit only
  // carries ISSUE_* events (an action's approve/execute is audited against the action resource,
  // not retroactively against the issue that spawned it) -- the dashboard's org-wide recent
  // activity feed is where both show up together.
  const issueAfter = await api(token, "GET", `/api/operations/issues/${issueId}`);
  const issueAuditTypes = (issueAfter.json.audit || []).map((e) => e.event_type);
  check(
    "the issue's own audit log records its lifecycle",
    issueAuditTypes.includes("ISSUE_CREATED") &&
      issueAuditTypes.includes("ISSUE_RESOLVED") &&
      issueAuditTypes.includes("ISSUE_REOPENED"),
    issueAuditTypes.join(","),
  );
  const dashboardData = await api(token, "GET", "/api/operations/dashboard");
  const recentTypes = (dashboardData.json.recent_activity || []).map((e) => e.event_type);
  check(
    "org-wide recent activity records the action's approval and execution",
    recentTypes.includes("ACTION_APPROVED") && recentTypes.includes("ACTION_EXECUTED"),
    recentTypes.join(","),
  );

  // --- 16: tenant isolation -- a second business can never see this issue
  const ctx2 = await browser.newContext();
  const page2 = await ctx2.newPage();
  const email2 = `ops-e2e-other-${stamp}@example.com`;
  await signup(page2, email2, "Other Ops Brand");
  const token2 = await apiToken(email2);
  const crossTenant = await api(token2, "GET", `/api/operations/issues/${issueId}`);
  check("a different tenant's API call for this issue is 404", crossTenant.status === 404, crossTenant.status);
  await page2.goto(`${BASE}/operations/issues/${issueId}`);
  const page2Body = await page2.textContent("body");
  check("a different tenant's browser never sees this issue's title", !page2Body.includes(`E2E urgent issue ${stamp}`));
  await ctx2.close();

  check("no uncaught browser errors", errors.length === 0, errors.join(" | "));
  await browser.close();
  console.log(failed === 0 ? "\nALL E2E CHECKS PASSED" : `\n${failed} E2E CHECK(S) FAILED`);
  process.exit(failed === 0 ? 0 : 1);
})().catch((e) => {
  console.error("E2E crashed:", e);
  process.exit(2);
});
