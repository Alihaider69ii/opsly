// Browser coverage for workflow lifecycle, lead event execution, failed delivery inspection,
// and execution tenant isolation.
const cfg = require("./lib/config");

const { BASE, API } = cfg;
const stamp = Date.now();
const password = "correct-horse-battery";
let failed = 0;

function check(name, ok, extra = "") {
  if (!ok) failed++;
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${extra ? `   [${String(extra).slice(0, 240)}]` : ""}`);
}

async function signup(page, email, business) {
  await page.goto(`${BASE}/signup`);
  await page.fill("#email", email);
  await page.fill("#password", password);
  await Promise.all([page.waitForURL(/\/onboarding$/), page.click("button[type=submit]")]);
  await page.fill("#name", business);
  await Promise.all([page.waitForURL(/\/dashboard$/), page.click("button[type=submit]")]);
  await page.waitForSelector("h1:has-text('Welcome back')");
}

async function eventually(checkFn, timeout = 45000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    const result = await checkFn();
    if (result) return result;
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  throw new Error("timed out waiting for the automation execution");
}

(async () => {
  const browser = await cfg.launchBrowser();
  try {
    const context = await browser.newContext({ viewport: { width: 1100, height: 900 } });
    const page = await context.newPage();
    const business = `Automation E2E ${stamp}`;
    const email = `automation-${stamp}@example.com`;
    await signup(page, email, business);
    // The dashboard creates the chat widget on its settings route; public lead capture then uses
    // exactly the visitor key and conversation-token path used by the real widget.
    await page.goto(`${BASE}/website-chat`);
    await page.goto(`${BASE}/automations`);

    const successfulName = `Lead task ${stamp}`;
    await page.getByLabel("Name", { exact: false }).fill(successfulName);
    await page.getByLabel("Trigger").selectOption("website_lead_captured");
    await page.getByLabel("First action").selectOption("create_task");
    await page.getByLabel("Action text / objective").fill("Review captured lead");
    await page.getByRole("button", { name: "Save draft" }).click();
    await page.getByRole("status").getByText("Workflow saved as a draft.").waitFor();
    await page.reload();
    const card = page.locator("li").filter({ hasText: successfulName });
    await card.getByRole("button", { name: "Activate" }).click();
    await card.getByText("ACTIVE").waitFor();
    check("workflow can be created and activated", true);
    await card.getByRole("link", { name: "Inspect configuration and history" }).click();
    await page.getByRole("heading", { name: successfulName }).waitFor();
    check("workflow configuration can be inspected", page.url().includes("/automations/"));
    const workflowPath = new URL(page.url()).pathname;
    const workflowId = workflowPath.split("/").at(-1);

    const session = (await context.cookies(BASE)).find((cookie) => cookie.name === "bizflow_session");
    const orgRow = cfg.sql(
      `SELECT o.id::text || '|' || w.public_key FROM users u JOIN organization_members m ON m.user_id=u.id JOIN organizations o ON o.id=m.organization_id JOIN chat_widgets w ON w.organization_id=o.id WHERE u.email='${email}'`,
    );
    const [orgId, publicKey] = orgRow.split("|");
    check("the lead-capture test is bound to this tenant's widget", Boolean(orgId && publicKey));

    const failedName = `Unconfigured message ${stamp}`;
    const createFailed = await fetch(`${API}/api/automations`, {
      method: "POST",
      headers: {
        authorization: `Bearer ${session.value}`,
        "x-organization-id": orgId,
        "content-type": "application/json",
      },
      body: JSON.stringify({
        name: failedName,
        definition: {
          trigger: { event: "website_lead_captured" },
          conditions: [],
          actions: [{ type: "send_message", channel: "website", content: "Follow up" }],
        },
      }),
    });
    const failedWorkflow = await createFailed.json();
    check("manager API accepts a valid provider-independent message action", createFailed.status === 201, `${createFailed.status} ${JSON.stringify(failedWorkflow)}`);
    const activateFailed = await fetch(`${API}/api/automations/${failedWorkflow.id}/activate`, {
      method: "POST",
      headers: { authorization: `Bearer ${session.value}`, "x-organization-id": orgId },
    });
    check("the second workflow activates", activateFailed.status === 200, String(activateFailed.status));

    const conversationResponse = await fetch(`${API}/api/public/chat/${publicKey}/conversations`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ visitor_id: `automation_${stamp}` }),
    });
    const conversation = await conversationResponse.json();
    const leadResponse = await fetch(`${API}/api/public/chat/${publicKey}/leads`, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "x-conversation-token": conversation.token,
      },
      body: JSON.stringify({
        conversation_id: conversation.conversation_id,
        name: "Voluntary E2E lead",
        email: `lead-${stamp}@example.com`,
        marketing_consent: true,
      }),
    });
    check("voluntary website lead capture is accepted", leadResponse.status === 202, `${leadResponse.status} ${await leadResponse.text()}`);

    const statusRows = await eventually(() => {
      const result = cfg.sql(
        `SELECT w.name || ':' || e.status FROM automation_executions e JOIN automation_workflows w ON w.id=e.workflow_id WHERE w.name IN ('${successfulName}','${failedName}') ORDER BY w.name`,
      );
      const lines = result.split("\n").filter(Boolean);
      return lines.length === 2 ? lines : null;
    });
    check("lead event creates a completed internal task execution", statusRows.includes(`${successfulName}:COMPLETED`), statusRows.join(", "));
    check("unconfigured messaging is visibly failed, never reported delivered", statusRows.includes(`${failedName}:FAILED`), statusRows.join(", "));

    await page.goto(`${BASE}${workflowPath}`);
    await eventually(async () => {
      await page.reload();
      return (await page.locator("a[href^='/automations/executions/']").count()) > 0;
    });
    await page.locator("a[href^='/automations/executions/']").first().click();
    await page.getByRole("heading", { name: "Execution" }).waitFor();
    check("execution detail and step history are inspectable", (await page.locator("body").innerText()).includes("COMPLETED"));
    const executionPath = new URL(page.url()).pathname;
    const executionId = executionPath.split("/").at(-1);

    const otherContext = await browser.newContext({ viewport: { width: 1000, height: 800 } });
    const otherPage = await otherContext.newPage();
    await signup(otherPage, `other-${stamp}@example.com`, `Other Automation ${stamp}`);
    const foreignResponse = await otherPage.goto(`${BASE}${executionPath}`);
    await otherPage.waitForFunction(
      () => !document.body.innerText.includes("Loading execution"),
      null,
      { timeout: 15000 },
    );
    const foreignText = await otherPage.locator("body").innerText();
    const notFound = foreignResponse?.status() === 404 || /This page could not be found/i.test(foreignText);
    check("another tenant receives a not-found page for this execution", notFound, `HTTP ${foreignResponse?.status()} body=${foreignText.slice(0, 180)}`);
    check("the foreign execution id is not rendered", !foreignText.includes(executionId));
    await otherContext.close();
    await context.close();
  } catch (error) {
    failed++;
    console.log("FAIL  unexpected automation E2E error:", error.stack || error);
  } finally {
    await browser.close();
  }
  console.log(failed ? `\n${failed} AUTOMATION E2E CHECK(S) FAILED` : "\nALL AUTOMATION E2E CHECKS PASSED");
  process.exit(failed ? 1 : 0);
})().catch((error) => {
  console.error("Automation E2E crashed:", error);
  process.exit(2);
});
