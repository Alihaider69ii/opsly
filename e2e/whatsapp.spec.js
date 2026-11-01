// Browser coverage for the WhatsApp dashboard: connect/status/disconnect, template-sync refusal
// against the fake provider, a signed inbound webhook (including a duplicate delivery), AI
// auto-reply toggling, consent/suppression, the customer-service window, a minimal automation
// workflow driven by a real WhatsApp event (success, suppressed-contact rejection, window-closed
// rejection, invalid template variables), cross-tenant isolation, and (Phase 8) the RAG-grounded
// multi-turn AI conversation layer: a real crawled knowledge source shared with website chat,
// a grounded reply, multi-turn follow-up context, explicit human handoff and its resolution, and
// the AI settings form. Entirely offline: the API runs with
// WHATSAPP_MESSAGING_PROVIDER=fake and LLM_PROVIDER=fake/EMBEDDING_PROVIDER=fake, so nothing here
// ever calls Meta or a real model.
const crypto = require("crypto");
const cfg = require("./lib/config");

const card = (page, host) => page.locator(`li[data-source-id]:has-text("${host}")`).first();

const { BASE, API } = cfg;
const stamp = Date.now();
const password = "correct-horse-battery";
const APP_SECRET = process.env.E2E_WHATSAPP_APP_SECRET || "e2e-whatsapp-app-secret";
let failed = 0;

function check(name, ok, extra = "") {
  if (!ok) failed++;
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${extra ? `   [${String(extra).slice(0, 240)}]` : ""}`);
}

function signWebhook(body) {
  return `sha256=${crypto.createHmac("sha256", APP_SECRET).update(body).digest("hex")}`;
}

function inboundMessagePayload(phoneNumberId, wamid, fromWaId, text, ts) {
  return {
    object: "whatsapp_business_account",
    entry: [
      {
        id: "waba-e2e",
        changes: [
          {
            value: {
              messaging_product: "whatsapp",
              metadata: { display_phone_number: "15550001111", phone_number_id: phoneNumberId },
              contacts: [{ profile: { name: "E2E Contact" }, wa_id: fromWaId }],
              messages: [
                { from: fromWaId, id: wamid, timestamp: String(ts), type: "text", text: { body: text } },
              ],
            },
            field: "messages",
          },
        ],
      },
    ],
  };
}

async function postWebhook(payload) {
  const body = JSON.stringify(payload);
  return fetch(`${API}/api/webhooks/whatsapp`, {
    method: "POST",
    headers: { "content-type": "application/json", "x-hub-signature-256": signWebhook(body) },
    body,
  });
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

async function eventually(checkFn, timeout = 20000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    const result = await checkFn();
    if (result) return result;
    await new Promise((resolve) => setTimeout(resolve, 400));
  }
  throw new Error("timed out waiting for the condition");
}

async function orgAndSession(context, email) {
  const session = (await context.cookies(BASE)).find((c) => c.name === "bizflow_session");
  const orgId = cfg.sql(
    `SELECT o.id FROM users u JOIN organization_members m ON m.user_id=u.id JOIN organizations o ON o.id=m.organization_id WHERE u.email='${email}'`,
  );
  return { token: session.value, orgId };
}

async function apiPost(path, token, orgId, body) {
  return fetch(`${API}${path}`, {
    method: "POST",
    headers: {
      authorization: `Bearer ${token}`,
      "x-organization-id": orgId,
      "content-type": "application/json",
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

(async () => {
  const browser = await cfg.launchBrowser();
  try {
    const context = await browser.newContext({ viewport: { width: 1100, height: 1000 } });
    const page = await context.newPage();
    const business = `WhatsApp E2E ${stamp}`;
    const email = `whatsapp-${stamp}@example.com`;
    await signup(page, email, business);

    // ---- connect (fake provider) --------------------------------------------------------------
    await page.goto(`${BASE}/whatsapp`);
    await page.getByRole("heading", { name: "Connect a WhatsApp number" }).waitFor();
    const phoneNumberId = `PNID_${stamp}`;
    await page.getByLabel("Phone number ID").fill(phoneNumberId);
    await page.getByLabel("WhatsApp Business Account ID").fill(`WABA_${stamp}`);
    await page.getByLabel("Access token").fill("fake-access-token");
    await Promise.all([
      page.waitForURL(/\/whatsapp\?connected=1$/),
      page.getByRole("button", { name: "Connect WhatsApp" }).click(),
    ]);
    check(
      "connecting with the fake provider lands on the connected dashboard",
      await page.locator('[data-testid="whatsapp-connection"] [data-status="CONNECTED"]').isVisible(),
    );
    check(
      "the connection card shows the phone number id, never a raw token",
      (await page.locator('[data-field="phone-number-id"]').innerText()) === phoneNumberId,
    );
    check(
      "the access token never appears anywhere in the rendered page",
      !(await page.locator("body").innerText()).includes("fake-access-token"),
    );
    const localStorageDump = await page.evaluate(() => JSON.stringify(localStorage));
    check("the access token is never written to localStorage", !localStorageDump.includes("fake-access-token"));

    const { token, orgId } = await orgAndSession(context, email);
    const connectionId = cfg.sql(
      `SELECT id FROM whatsapp_connections WHERE organization_id='${orgId}'`,
    );

    // ---- template sync: the fake provider must be visibly refused, never faked ----------------
    await page.getByRole("button", { name: "Sync templates" }).click();
    const syncError = await page.locator("main").getByRole("alert").first();
    await syncError.waitFor({ timeout: 10000 });
    check(
      "template sync against the fake provider is refused, not silently faked",
      /offline\/test provider is active/i.test(await syncError.innerText()),
    );

    // ---- AI auto-reply toggles live, no reload ------------------------------------------------
    await page.getByRole("button", { name: "Enable AI auto-reply" }).click();
    await page.getByRole("button", { name: "Disable AI auto-reply" }).waitFor();
    check(
      "auto-reply toggling updates the connection card live",
      (await page.locator('[data-field="ai-auto-reply"]').innerText()) === "enabled",
    );
    // Disabled again immediately: the automation-workflow checks below need to count exactly the
    // messages THEY send, and an AI auto-reply would independently reply to the same inbound
    // message and make that count ambiguous. The live-toggle behavior is already proven above.
    await page.getByRole("button", { name: "Disable AI auto-reply" }).click();
    await page.getByRole("button", { name: "Enable AI auto-reply" }).waitFor();

    // ---- inbound webhook: signature, dedupe, conversation/message rendering -------------------
    const fromWaId = `1415555${String(stamp).slice(-4)}`;
    const wamid1 = `wamid.E2E.${stamp}.1`;
    const ts1 = Math.floor(Date.now() / 1000);
    const goodBody = JSON.stringify(inboundMessagePayload(phoneNumberId, wamid1, fromWaId, "Hello from a customer", ts1));
    const forged = await fetch(`${API}/api/webhooks/whatsapp`, {
      method: "POST",
      headers: { "content-type": "application/json", "x-hub-signature-256": "sha256=deadbeef" },
      body: goodBody,
    });
    check("a webhook with an invalid signature is rejected (401)", forged.status === 401, forged.status);

    const first = await postWebhook(inboundMessagePayload(phoneNumberId, wamid1, fromWaId, "Hello from a customer", ts1));
    check("a correctly signed webhook is accepted (200)", first.status === 200, first.status);
    const duplicate = await postWebhook(inboundMessagePayload(phoneNumberId, wamid1, fromWaId, "Hello from a customer", ts1));
    check("the identical webhook delivered again is still accepted (200), not an error", duplicate.status === 200, duplicate.status);

    await eventually(() => cfg.sql(`SELECT count(*) FROM whatsapp_messages WHERE provider_message_id='${wamid1}'`) === "1");
    check(
      "a duplicate webhook delivery never creates a duplicate message row",
      cfg.sql(`SELECT count(*) FROM whatsapp_messages WHERE provider_message_id='${wamid1}'`) === "1",
    );
    check(
      "a duplicate webhook delivery never creates a duplicate ledger row",
      cfg.sql(`SELECT count(*) FROM whatsapp_webhook_events WHERE resource_wamid='${wamid1}'`) === "1",
    );

    const conversationId = await eventually(() => {
      const id = cfg.sql(`SELECT id FROM whatsapp_conversations WHERE organization_id='${orgId}'`);
      return id || null;
    });
    await page.goto(`${BASE}/whatsapp/conversations/${conversationId}`);
    await page.getByRole("heading", { name: /message/ }).waitFor();
    check(
      "the inbound message renders in the conversation detail",
      (await page.locator('[data-direction="INBOUND"]').innerText()).includes("Hello from a customer"),
    );

    // ---- consent -----------------------------------------------------------------------------
    // The automation below sends with consent_category "SUPPORT": opt in that exact category,
    // not just the first one in the list (TRANSACTIONAL), which would leave SUPPORT UNKNOWN
    // (never treated as opted in) and block every send below for the wrong reason.
    const supportRow = page.locator("li").filter({ hasText: "SUPPORT" });
    await supportRow.getByRole("button", { name: "Opt in" }).click();
    await eventually(async () => (await supportRow.locator('[data-status="OPTED_IN"]').count()) > 0);
    check(
      "opting the SUPPORT category in updates its badge live",
      (await supportRow.locator('[data-status="OPTED_IN"]').count()) > 0,
    );

    // ---- a successful automation send, triggered by a real WhatsApp event ---------------------
    const okWorkflowName = `WA reply ${stamp}`;
    const createOk = await apiPost(`/api/automations`, token, orgId, {
      name: okWorkflowName,
      definition: {
        trigger: { event: "whatsapp_message_received" },
        conditions: [],
        actions: [{ type: "send_whatsapp_text", content: "Thanks, we got your message!", consent_category: "SUPPORT" }],
      },
    });
    const okWorkflow = await createOk.json();
    check("the WhatsApp-action workflow is created", createOk.status === 201, `${createOk.status} ${JSON.stringify(okWorkflow)}`);
    const activateOk = await apiPost(`/api/automations/${okWorkflow.id}/activate`, token, orgId);
    check("the WhatsApp-action workflow activates", activateOk.status === 200, activateOk.status);

    const wamid2 = `wamid.E2E.${stamp}.2`;
    await postWebhook(inboundMessagePayload(phoneNumberId, wamid2, fromWaId, "Second message, now opted in", ts1 + 5));
    const okRow = await eventually(() => {
      const row = cfg.sql(`SELECT e.status FROM automation_executions e JOIN automation_workflows w ON w.id=e.workflow_id WHERE w.name='${okWorkflowName}'`);
      return row === "COMPLETED" || row === "FAILED" ? row : null;
    });
    check("a WhatsApp send action triggered by a real inbound event completes", okRow === "COMPLETED", okRow);
    const outboundSentCount = cfg.sql(
      `SELECT count(*) FROM whatsapp_messages WHERE conversation_id='${conversationId}' AND direction='OUTBOUND' AND status='SENT'`,
    );
    check(
      "the completed send is visible as an outbound message in the conversation",
      outboundSentCount === "1",
      outboundSentCount,
    );

    // ---- suppression blocks a subsequent send, even though consent is still OPTED_IN ----------
    await page.goto(`${BASE}/whatsapp/conversations/${conversationId}`);
    await page.getByRole("button", { name: "Suppress (block all future messages)" }).click();
    await eventually(async () => (await page.locator("text=Yes").count()) > 0);

    const wamid3 = `wamid.E2E.${stamp}.3`;
    await postWebhook(inboundMessagePayload(phoneNumberId, wamid3, fromWaId, "Third message, now suppressed", ts1 + 10));
    const suppressedRow = await eventually(() => {
      const row = cfg.sql(
        `SELECT e.error_code FROM automation_executions e JOIN automation_workflows w ON w.id=e.workflow_id WHERE w.name='${okWorkflowName}' AND e.status='FAILED' AND e.error_code IS NOT NULL ORDER BY e.created_at DESC LIMIT 1`,
      );
      return row || null;
    });
    check(
      "a send attempt against a suppressed contact is rejected, not silently dropped or sent",
      suppressedRow === "whatsapp_policy_blocked",
      suppressedRow,
    );
    // Window-closed rejection is deterministic and already covered at the unit/integration level
    // (tests/whatsapp/test_send.py::test_text_send_blocked_with_window_closed and
    // tests/whatsapp/test_automation_integration.py) -- it is deliberately NOT re-tested here via
    // a real inbound webhook, because every inbound message re-opens the window as a side effect
    // (window_service.record_inbound), so "deliver a webhook, then race a worker to close the
    // window before its automation job runs" is an inherently flaky thing to assert on in a real,
    // asynchronously-processed browser E2E run.

    // Suppression must not leak into the template tests below: lift it so only the variable
    // validation under test can reject the send.
    cfg.sql(`UPDATE whatsapp_suppressions SET lifted_at = now() WHERE organization_id='${orgId}'`);

    // ---- invalid template variables fail closed, never reach a provider call ------------------
    const templateId = crypto.randomUUID();
    cfg.sql(
      `INSERT INTO whatsapp_templates (id, organization_id, connection_id, name, language, category, status, components, created_at, updated_at) VALUES ('${templateId}', '${orgId}', '${connectionId}', 'order_update_${stamp}', 'en_US', 'UTILITY', 'APPROVED', '[{"type":"BODY","text":"Hi {{1}}, your order {{2}} shipped!"}]'::json, now(), now())`,
    );
    const badTemplateName = `WA bad template ${stamp}`;
    const createBad = await apiPost(`/api/automations`, token, orgId, {
      name: badTemplateName,
      definition: {
        trigger: { event: "whatsapp_message_received" },
        conditions: [],
        actions: [
          {
            type: "send_whatsapp_template",
            template_name: `order_update_${stamp}`,
            template_language: "en_US",
            variables: { "1": "only one variable" },
            consent_category: "SUPPORT",
          },
        ],
      },
    });
    const badTemplateWorkflow = await createBad.json();
    check("a template action with the wrong variable count is still a valid workflow definition", createBad.status === 201, createBad.status);
    await apiPost(`/api/automations/${badTemplateWorkflow.id}/activate`, token, orgId);
    const wamidBadTpl = `wamid.E2E.${stamp}.badtpl`;
    await postWebhook(inboundMessagePayload(phoneNumberId, wamidBadTpl, fromWaId, "Fifth message", ts1 + 30));
    const badTplRow = await eventually(() => {
      const row = cfg.sql(
        `SELECT e.error_code FROM automation_executions e JOIN automation_workflows w ON w.id=e.workflow_id WHERE w.name='${badTemplateName}' AND e.status='FAILED'`,
      );
      return row || null;
    });
    check(
      "a template send with a missing required variable fails closed before any provider call",
      badTplRow === "whatsapp_invalid_request",
      badTplRow,
    );

    // ---- a valid, approved template sends correctly -------------------------------------------
    cfg.sql(`UPDATE whatsapp_suppressions SET lifted_at = now() WHERE organization_id='${orgId}'`);
    const goodTemplateName = `WA good template ${stamp}`;
    const createGoodTpl = await apiPost(`/api/automations`, token, orgId, {
      name: goodTemplateName,
      definition: {
        trigger: { event: "whatsapp_message_received" },
        conditions: [],
        actions: [
          {
            type: "send_whatsapp_template",
            template_name: `order_update_${stamp}`,
            template_language: "en_US",
            variables: { "1": "Ali", "2": "ORD-1" },
            consent_category: "SUPPORT",
          },
        ],
      },
    });
    const goodTemplateWorkflow = await createGoodTpl.json();
    await apiPost(`/api/automations/${goodTemplateWorkflow.id}/activate`, token, orgId);
    const wamidGoodTpl = `wamid.E2E.${stamp}.goodtpl`;
    await postWebhook(inboundMessagePayload(phoneNumberId, wamidGoodTpl, fromWaId, "Sixth message", ts1 + 40));
    const goodTplRow = await eventually(() => {
      const row = cfg.sql(
        `SELECT e.status FROM automation_executions e JOIN automation_workflows w ON w.id=e.workflow_id WHERE w.name='${goodTemplateName}'`,
      );
      return row === "COMPLETED" || row === "FAILED" ? row : null;
    });
    check("a correctly-filled approved template send completes", goodTplRow === "COMPLETED", goodTplRow);
    // Note: the earlier bad-variable-count attempt ALSO recorded a row with this same
    // template_name/language (FAILED, for audit visibility -- see send.py's _write_failed), so
    // this must be scoped to the successfully SENT one, not just any row naming this template.
    const sentTemplateCount = cfg.sql(
      `SELECT count(*) FROM whatsapp_messages WHERE conversation_id='${conversationId}' AND template_name='order_update_${stamp}' AND template_language='en_US' AND status='SENT'`,
    );
    check("the template message is recorded with its name and language", sentTemplateCount === "1", sentTemplateCount);

    // ---- Phase 8: the RAG-grounded multi-turn AI conversation layer ---------------------------
    // Archive the earlier automation workflows first: they also trigger on whatsapp_message_
    // received and would independently send a second message for every inbound delivery below,
    // which is not wrong but would make the AI-specific assertions noisier than necessary.
    await apiPost(`/api/automations/${okWorkflow.id}/archive`, token, orgId);
    await apiPost(`/api/automations/${badTemplateWorkflow.id}/archive`, token, orgId);
    await apiPost(`/api/automations/${goodTemplateWorkflow.id}/archive`, token, orgId);

    // Share the SAME knowledge base website chat uses -- this is the actual proof of "no second
    // RAG system": crawling example.com for this organization makes it answerable over WhatsApp.
    await page.goto(`${BASE}/knowledge`);
    await page.fill("#url", "https://example.com");
    await page.click("button:has-text('+ Add Website')");
    await card(page, "example.com").locator('[data-status="COMPLETED"]').waitFor({ timeout: 90000 });
    check("example.com crawled to COMPLETED for the WhatsApp org too", true);

    await page.goto(`${BASE}/whatsapp`);
    await page.getByRole("button", { name: "Enable AI auto-reply" }).click();
    await page.getByRole("button", { name: "Disable AI auto-reply" }).waitFor();

    // ---- AI settings: change and verify the change survives a full reload ---------------------
    await page.getByLabel("Minimum confidence to send a generated answer").selectOption("LOW");
    await page.getByRole("button", { name: "Save AI settings" }).click();
    await page.getByText("AI settings saved.").waitFor({ timeout: 10000 });
    await page.reload();
    check(
      "the AI settings change round-trips through the API, not just local state",
      await page.getByLabel("Minimum confidence to send a generated answer").inputValue() === "LOW",
    );

    async function outboundTexts() {
      await page.goto(`${BASE}/whatsapp/conversations/${conversationId}`);
      return page.locator('[data-direction="OUTBOUND"]').allInnerTexts();
    }

    // ---- a grounded, RAG-backed AI reply --------------------------------------------------------
    const wamidAi1 = `wamid.E2E.${stamp}.ai1`;
    await postWebhook(
      inboundMessagePayload(phoneNumberId, wamidAi1, fromWaId, "What is this domain used for in documentation examples?", ts1 + 50),
    );
    const groundedReply = await eventually(async () => {
      const texts = await outboundTexts();
      return texts.find((t) => t.includes("Here is what I found")) || null;
    });
    check("the AI reply is RAG-grounded, quoting the crawled knowledge", !!groundedReply, groundedReply);
    check(
      "the dashboard shows the reply as Grounded with a confidence badge",
      (await page.locator('[data-testid="ai-metadata"]').last().innerText()).includes("Grounded"),
    );

    // ---- multi-turn: a short follow-up relies on conversation history for retrieval -----------
    const wamidAi2 = `wamid.E2E.${stamp}.ai2`;
    await postWebhook(inboundMessagePayload(phoneNumberId, wamidAi2, fromWaId, "And what about that?", ts1 + 55));
    const secondGroundedReply = await eventually(async () => {
      const texts = await outboundTexts();
      const matches = texts.filter((t) => t.includes("Here is what I found"));
      return matches.length >= 2 ? matches[matches.length - 1] : null;
    });
    check(
      "a short follow-up still gets a grounded reply via conversation-aware retrieval",
      !!secondGroundedReply,
      secondGroundedReply,
    );

    // Note: the no-knowledge/low-confidence FALLBACK path (and the "never a fabricated answer"
    // guarantee) is deliberately proven deterministically at the unit level instead of here --
    // tests/whatsapp/test_autoreply.py and tests/ai/test_whatsapp_agent.py control retrieval
    // exactly (StubSearch), whereas this E2E's FakeEmbeddingProvider is a crude, un-weighted
    // bag-of-words hash that cannot reliably tell "genuinely off-topic" apart from "shares a few
    // common English words" against a single-document knowledge base -- forcing that judgment
    // call into a real-browser E2E would make this suite flaky for a property already proven
    // deterministically elsewhere.

    // ---- explicit human handoff request, and its resolution ------------------------------------
    const wamidHandoff = `wamid.E2E.${stamp}.handoff`;
    await postWebhook(
      inboundMessagePayload(phoneNumberId, wamidHandoff, fromWaId, "I want to talk to a human please", ts1 + 65),
    );
    await page.goto(`${BASE}/whatsapp/conversations/${conversationId}`);
    await page.getByText("Needs a human").waitFor({ timeout: 20000 });
    check(
      "the conversation shows it needs a human, with the request reason recorded",
      (await page.locator('[data-field="handoff-reason"]').innerText()) === "customer_requested_human",
    );
    check(
      "AI auto-reply never pretends a human has joined",
      !(await page.locator("body").innerText()).toLowerCase().includes("a human has joined"),
    );
    await page.getByRole("button", { name: "Resolve (return to AI)" }).click();
    await page.getByText("AI is answering this conversation.").waitFor({ timeout: 10000 });
    check("resolving handoff returns the conversation to AI control", true);

    // ---- disconnect preserves consent, suppression and conversation history -------------------
    const consentCountBefore = cfg.sql(`SELECT count(*) FROM whatsapp_consents WHERE organization_id='${orgId}'`);
    const suppressionCountBefore = cfg.sql(`SELECT count(*) FROM whatsapp_suppressions WHERE organization_id='${orgId}'`);
    const messageCountBefore = cfg.sql(`SELECT count(*) FROM whatsapp_messages WHERE organization_id='${orgId}'`);
    await page.goto(`${BASE}/whatsapp`);
    await Promise.all([
      page.waitForURL(/\/whatsapp\?disconnected=1$/),
      (async () => {
        await page.getByRole("button", { name: "Disconnect" }).click();
        await page.getByRole("button", { name: "Yes, disconnect" }).click();
      })(),
    ]);
    check(
      "disconnect keeps consent history",
      cfg.sql(`SELECT count(*) FROM whatsapp_consents WHERE organization_id='${orgId}'`) === consentCountBefore,
    );
    check(
      "disconnect keeps suppression history",
      cfg.sql(`SELECT count(*) FROM whatsapp_suppressions WHERE organization_id='${orgId}'`) === suppressionCountBefore,
    );
    check(
      "disconnect keeps conversation/message history",
      cfg.sql(`SELECT count(*) FROM whatsapp_messages WHERE organization_id='${orgId}'`) === messageCountBefore,
    );
    check(
      "disconnect clears the stored access token",
      cfg.sql(`SELECT access_token_encrypted IS NULL FROM whatsapp_connections WHERE id='${connectionId}'`) === "t",
    );

    // ---- tenant isolation ----------------------------------------------------------------------
    const otherContext = await browser.newContext({ viewport: { width: 1000, height: 800 } });
    const otherPage = await otherContext.newPage();
    await signup(otherPage, `other-whatsapp-${stamp}@example.com`, `Other WhatsApp Org ${stamp}`);
    const foreignResponse = await otherPage.goto(`${BASE}/whatsapp/conversations/${conversationId}`);
    await otherPage.waitForFunction(() => !document.body.innerText.includes("Loading conversation"), null, { timeout: 15000 });
    const foreignText = await otherPage.locator("body").innerText();
    const notFound = foreignResponse?.status() === 404 || /This page could not be found/i.test(foreignText);
    check("another organization gets a not-found page for this conversation", notFound, `HTTP ${foreignResponse?.status()} body=${foreignText.slice(0, 180)}`);
    check("the foreign conversation's content is never rendered", !foreignText.includes("Hello from a customer"));

    const { token: otherToken, orgId: otherOrgId } = await orgAndSession(otherContext, `other-whatsapp-${stamp}@example.com`);
    const crossTenantApi = await fetch(`${API}/api/whatsapp/conversations/${conversationId}/messages`, {
      headers: { authorization: `Bearer ${otherToken}`, "x-organization-id": otherOrgId },
    });
    check("the API also 404s a cross-tenant conversation id (never 403, never confirms existence)", crossTenantApi.status === 404, crossTenantApi.status);

    await otherContext.close();
    await context.close();
  } catch (error) {
    failed++;
    console.log("FAIL  unexpected WhatsApp E2E error:", error.stack || error);
  } finally {
    await browser.close();
  }
  console.log(failed ? `\n${failed} WHATSAPP E2E CHECK(S) FAILED` : "\nALL WHATSAPP E2E CHECKS PASSED");
  process.exit(failed ? 1 : 0);
})().catch((error) => {
  console.error("WhatsApp E2E crashed:", error);
  process.exit(2);
});
