// Browser coverage for Phase 9: the unified support inbox across WhatsApp/website/email,
// priority/assignment/AI-human switching/resolve/reopen, the customer timeline, email
// templates/campaigns, marketing suppression, email webhook signature/dedup/status updates, and
// cross-tenant isolation. Entirely offline: EMAIL_PROVIDER=fake and WHATSAPP_MESSAGING_PROVIDER=
// fake, so nothing here ever calls Resend or Meta. The fake email provider's "fetch inbound
// content" registry lives in Redis (shared across the API/worker processes, see
// app/services/email/providers/fake.py) -- this suite seeds it directly via redis-cli, exactly as
// cfg.sql lets other suites assert against Postgres directly.
const crypto = require("crypto");
const cfg = require("./lib/config");

const { BASE, API } = cfg;
const stamp = Date.now();
const password = "correct-horse-battery";
const EMAIL_WEBHOOK_SECRET = process.env.E2E_EMAIL_WEBHOOK_SECRET || "whsec_ZTJlLWVtYWlsLXdlYmhvb2stc2VjcmV0";
const WHATSAPP_APP_SECRET = process.env.E2E_WHATSAPP_APP_SECRET || "e2e-whatsapp-app-secret";
let failed = 0;

function check(name, ok, extra = "") {
  if (!ok) failed++;
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${extra ? `   [${String(extra).slice(0, 240)}]` : ""}`);
}

function signSvix(svixId, svixTimestamp, body) {
  const keyMaterial = EMAIL_WEBHOOK_SECRET.replace(/^whsec_/, "");
  const key = Buffer.from(keyMaterial, "base64");
  const signedContent = Buffer.concat([Buffer.from(`${svixId}.${svixTimestamp}.`), Buffer.from(body)]);
  const signature = crypto.createHmac("sha256", key).update(signedContent).digest("base64");
  return `v1,${signature}`;
}

async function postEmailWebhook(payload, { svixId, svixTimestamp, signatureOverride } = {}) {
  const body = JSON.stringify(payload);
  const id = svixId || `msg_${crypto.randomUUID()}`;
  const ts = svixTimestamp || String(Math.floor(Date.now() / 1000));
  const signature = signatureOverride || signSvix(id, ts, body);
  return fetch(`${API}/api/webhooks/email`, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "svix-id": id,
      "svix-timestamp": ts,
      "svix-signature": signature,
    },
    body,
  });
}

function registerFakeInbound(providerMessageId, { textBody, htmlBody }) {
  cfg.redisSet(
    `bizflow:fake-email-inbound:${providerMessageId}`,
    JSON.stringify({
      text_body: textBody,
      html_body: htmlBody,
      message_id_header: null,
      in_reply_to_header: null,
      references_header: null,
    }),
    3600,
  );
}

function signWhatsappWebhook(body) {
  return `sha256=${crypto.createHmac("sha256", WHATSAPP_APP_SECRET).update(body).digest("hex")}`;
}

function whatsappInboundPayload(phoneNumberId, wamid, fromWaId, text, ts) {
  return {
    object: "whatsapp_business_account",
    entry: [
      {
        id: "waba-e2e-support",
        changes: [
          {
            value: {
              messaging_product: "whatsapp",
              metadata: { display_phone_number: "15550001111", phone_number_id: phoneNumberId },
              contacts: [{ profile: { name: "E2E WA Contact" }, wa_id: fromWaId }],
              messages: [{ from: fromWaId, id: wamid, timestamp: String(ts), type: "text", text: { body: text } }],
            },
            field: "messages",
          },
        ],
      },
    ],
  };
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

async function eventually(checkFn, timeout = 25000) {
  const deadline = Date.now() + timeout;
  let last;
  while (Date.now() < deadline) {
    last = await checkFn();
    if (last) return last;
    await new Promise((resolve) => setTimeout(resolve, 400));
  }
  throw new Error(`timed out waiting for the condition (last=${JSON.stringify(last)})`);
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

async function captureLead(publicKey, { name, email, marketingConsent }) {
  const created = await (
    await fetch(`${API}/api/public/chat/${publicKey}/conversations`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ visitor_id: `support_e2e_${stamp}_${Math.random().toString(36).slice(2)}` }),
    })
  ).json();
  const leadResponse = await fetch(`${API}/api/public/chat/${publicKey}/leads`, {
    method: "POST",
    headers: { "content-type": "application/json", "x-conversation-token": created.token },
    body: JSON.stringify({
      conversation_id: created.conversation_id,
      name,
      email,
      marketing_consent: marketingConsent,
    }),
  });
  return { leadResponse, conversationId: created.conversation_id, token: created.token };
}

(async () => {
  const browser = await cfg.launchBrowser();
  try {
    const context = await browser.newContext({ viewport: { width: 1200, height: 1000 } });
    const page = await context.newPage();
    const business = `Support E2E ${stamp}`;
    const email = `support-${stamp}@example.com`;
    await signup(page, email, business);
    const { token, orgId } = await orgAndSession(context, email);

    // ---- 1. support dashboard loads --------------------------------------------------------
    await page.goto(`${BASE}/support`);
    await page.getByText(/Every WhatsApp, website and email conversation in one inbox\./).waitFor();
    check("the support dashboard loads", true);

    // ---- 2. website conversation visible in the unified inbox -----------------------------
    await page.goto(`${BASE}/website-chat`);
    const publicKey = cfg.sql(`SELECT public_key FROM chat_widgets WHERE organization_id='${orgId}'`);
    check("a chat widget exists for this org", Boolean(publicKey));
    const webConv = await (
      await fetch(`${API}/api/public/chat/${publicKey}/conversations`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ visitor_id: `support_e2e_${stamp}` }),
      })
    ).json();
    const webMsgResp = await fetch(`${API}/api/public/chat/${publicKey}/messages`, {
      method: "POST",
      headers: { "content-type": "application/json", "x-conversation-token": webConv.token },
      body: JSON.stringify({ conversation_id: webConv.conversation_id, content: "Hi, I need help with my order" }),
    });
    check("the website message is accepted", webMsgResp.status === 200, webMsgResp.status);

    await page.goto(`${BASE}/support`);
    await eventually(async () => (await page.locator('li[data-channel="WEBSITE"]').count()) > 0);
    check("the website conversation is visible in the unified inbox", true);
    const webConversationId = cfg.sql(
      `SELECT id FROM support_conversations WHERE organization_id='${orgId}' AND channel='WEBSITE'`,
    );

    // ---- 3. WhatsApp conversation visible in the unified inbox -----------------------------
    await page.goto(`${BASE}/whatsapp`);
    const phoneNumberId = `PNID_SUP_${stamp}`;
    await page.getByLabel("Phone number ID").fill(phoneNumberId);
    await page.getByLabel("WhatsApp Business Account ID").fill(`WABA_SUP_${stamp}`);
    await page.getByLabel("Access token").fill("fake-access-token");
    await Promise.all([
      page.waitForURL(/\/whatsapp\?connected=1$/),
      page.getByRole("button", { name: "Connect WhatsApp" }).click(),
    ]);
    const waWamid = `wamid.SUPPORT.E2E.${stamp}`;
    const waFromId = `1415556${String(stamp).slice(-4)}`;
    const waBody = JSON.stringify(whatsappInboundPayload(phoneNumberId, waWamid, waFromId, "Hi from WhatsApp", Math.floor(Date.now() / 1000)));
    const waWebhookResp = await fetch(`${API}/api/webhooks/whatsapp`, {
      method: "POST",
      headers: { "content-type": "application/json", "x-hub-signature-256": signWhatsappWebhook(waBody) },
      body: waBody,
    });
    check("the signed WhatsApp webhook is accepted", waWebhookResp.status === 200, waWebhookResp.status);

    // The webhook response only confirms intake (ledger + queue); the worker processes it
    // asynchronously. /support is server-rendered once per navigation with no client polling, so
    // poll the (fast) backend state first and only navigate once it is actually there -- never
    // poll an already-loaded, frozen DOM waiting for server-side data that will never arrive.
    await eventually(() => cfg.sql(`SELECT count(*) FROM support_conversations WHERE organization_id='${orgId}' AND channel='WHATSAPP'`) === "1");
    await page.goto(`${BASE}/support`);
    check("the WhatsApp conversation is visible in the unified inbox", (await page.locator('li[data-channel="WHATSAPP"]').count()) > 0);

    // ---- 4. email conversation visible in the unified inbox, + duplicate webhook dedup -----
    await page.goto(`${BASE}/email`);
    await page.getByLabel("From address").fill(`support-${stamp}@acme.example`);
    await page.getByLabel("From name (optional)").fill("Acme Support");
    await page.getByRole("button", { name: "Connect email" }).click();
    await page.locator('[data-testid="email-connection"]').waitFor();
    check("email connects against the fake provider", true);

    const emailProviderMessageId = `fake-e2e-${stamp}`;
    registerFakeInbound(emailProviderMessageId, {
      textBody: "I need help with a recent order",
      htmlBody: "<p>I need help with a recent order</p>",
    });
    const inboundAddress = await page.locator('[data-field="inbound-address"]').innerText();
    const inboundPayload = {
      type: "email.received",
      created_at: new Date().toISOString(),
      data: {
        email_id: emailProviderMessageId,
        from: `customer-${stamp}@example.com`,
        to: [inboundAddress],
        subject: "Need help",
      },
    };
    const fixedSvixId = `msg_e2e_dup_${stamp}`;
    const fixedSvixTimestamp = String(Math.floor(Date.now() / 1000));
    const firstDelivery = await postEmailWebhook(inboundPayload, { svixId: fixedSvixId, svixTimestamp: fixedSvixTimestamp });
    check("the signed inbound email webhook is accepted (200)", firstDelivery.status === 200, firstDelivery.status);
    const duplicateDelivery = await postEmailWebhook(inboundPayload, { svixId: fixedSvixId, svixTimestamp: fixedSvixTimestamp });
    check("the identical webhook delivered again is still accepted (200), not an error", duplicateDelivery.status === 200, duplicateDelivery.status);

    await eventually(() => cfg.sql(`SELECT count(*) FROM email_messages WHERE organization_id='${orgId}' AND provider_message_id='${emailProviderMessageId}'`) === "1");
    check(
      "a duplicate inbound email webhook delivery never creates a duplicate message row",
      cfg.sql(`SELECT count(*) FROM email_messages WHERE organization_id='${orgId}' AND provider_message_id='${emailProviderMessageId}'`) === "1",
    );

    await eventually(() => cfg.sql(`SELECT count(*) FROM support_conversations WHERE organization_id='${orgId}' AND channel='EMAIL'`) === "1");
    await page.goto(`${BASE}/support`);
    check("the email conversation is visible in the unified inbox", (await page.locator('li[data-channel="EMAIL"]').count()) > 0);

    // ---- 5. customer timeline --------------------------------------------------------------
    await page.goto(`${BASE}/support/conversations/${webConversationId}`);
    await page.locator('ul[data-testid="support-messages"]').waitFor();
    const timelineEntries = await page.locator('ul[data-testid="timeline"] li[data-event]').count();
    check("the customer timeline shows at least one event", timelineEntries > 0, timelineEntries);

    // ---- 6. AI reply is marked AI (website's default auto-reply, no knowledge base) --------
    const aiMarked = await eventually(async () => {
      const count = await page.locator('li[data-message][data-sender-type="AI"]').count();
      return count > 0 ? count : null;
    });
    check("the automated reply to the website message is attributed to AI", Boolean(aiMarked));

    // ---- 7. priority change -----------------------------------------------------------------
    await page.locator('button[data-priority-option="URGENT"]').click();
    await eventually(async () => (await page.locator('[data-priority="URGENT"]').count()) > 0);
    check("changing priority updates the badge live", true);

    // ---- 8. assignment: claim / release -----------------------------------------------------
    await page.locator('button[data-action="claim"]').click();
    await page.locator('button[data-action="release"]').waitFor();
    check("claiming the conversation shows a Release control", true);
    await page.locator('button[data-action="release"]').click();
    await page.locator('button[data-action="claim"]').waitFor();
    check("releasing the conversation shows a Claim control again", true);
    await page.locator('button[data-action="claim"]').click();
    await page.locator('button[data-action="release"]').waitFor();

    // ---- 9. human takeover -------------------------------------------------------------------
    await page.locator('button[data-action="take-over"]').click();
    await eventually(async () => (await page.locator('[data-status="HUMAN_ACTIVE"]').count()) > 0);
    check("taking over moves the conversation to Human active", true);

    // ---- 10. human reply marked HUMAN -------------------------------------------------------
    // The panel remounts (key={id-updated_at}) right after take-over's server action resolves,
    // which resets any value already typed into the (freshly-mounted) textarea -- wait for that
    // to settle before filling it, matching how a real user pauses before typing anyway.
    await page.waitForTimeout(500);
    const replyText = "Thanks for reaching out, a teammate is here now.";
    await eventually(async () => {
      await page.locator('[data-testid="reply-textarea"]').fill(replyText);
      const value = await page.locator('[data-testid="reply-textarea"]').inputValue();
      return value === replyText ? true : null;
    });
    await page.locator('[data-testid="send-reply"]').click();
    const humanMarked = await eventually(async () => {
      const count = await page.locator('li[data-message][data-sender-type="HUMAN"]').count();
      return count > 0 ? count : null;
    });
    check("the human's reply is attributed to HUMAN", Boolean(humanMarked));

    // ---- 11. return to AI ---------------------------------------------------------------------
    await page.locator('button[data-action="return-to-ai"]').click();
    await eventually(async () => (await page.locator('[data-status="AI_ACTIVE"]').count()) > 0);
    check("returning to AI moves the conversation back to AI active", true);

    // ---- 12. resolve ----------------------------------------------------------------------------
    await page.locator('button[data-action="resolve"]').click();
    await eventually(async () => (await page.locator('[data-status="RESOLVED"]').count()) > 0);
    check("resolving the conversation updates its status", true);

    // ---- 13. reopen -----------------------------------------------------------------------------
    await page.locator('button[data-action="reopen"]').click();
    await eventually(async () => (await page.locator('[data-status="RESOLVED"]').count()) === 0);
    check("reopening the conversation clears the resolved status", true);

    // ---- 14/15. create and edit an email template ------------------------------------------
    const templateName = `Welcome ${stamp}`;
    await page.goto(`${BASE}/email/templates`);
    await page.locator('[data-testid="template-form"]').getByLabel("Name", { exact: true }).fill(templateName);
    await page.locator('[data-testid="template-form"]').getByLabel("Type").selectOption("MARKETING");
    await page.locator('[data-testid="template-form"]').getByLabel("Subject").fill("Welcome, {{customer.first_name}}!");
    await page.locator('[data-testid="template-form"]').getByLabel(/HTML body/).fill("<p>Hi {{customer.first_name}}, welcome!</p>");
    await page.locator('[data-testid="template-form"]').getByLabel(/Declared variables/).fill("customer.first_name");
    await page.locator('[data-testid="save-template"]').click();
    // Creating redirects straight to the new template's own detail page (router.push), never
    // stays on the list -- wait for that navigation rather than looking for a list row.
    await page.waitForURL(/\/email\/templates\/[0-9a-f-]{36}$/, { timeout: 15000 });
    check("an email template can be created", true);
    // The soft client-side navigation above leaves the Subject input present-but-not-actionable
    // for a long tail (observed, not fully explained); a plain reload gives a clean DOM.
    await page.reload();
    await page.locator('[data-testid="template-form"]').getByLabel("Subject").waitFor({ state: "visible" });

    await page.locator('[data-testid="template-form"]').getByLabel("Subject").fill("Welcome aboard, {{customer.first_name}}!");
    await page.locator('[data-testid="save-template"]').click();
    await eventually(async () => {
      const subjectValue = await page.locator('[data-testid="template-form"]').getByLabel("Subject").inputValue();
      return subjectValue.includes("Welcome aboard");
    });
    check("an email template can be edited", true);
    await page.locator('[data-testid="toggle-template-status"]').click();
    await page.getByText("Active", { exact: true }).waitFor();
    check("activating the template updates its status", true);
    const templateId = cfg.sql(`SELECT id FROM email_templates WHERE organization_id='${orgId}' AND name='${templateName}'`);

    // ---- 16/17/18. marketing campaign + suppression --------------------------------------
    const opted = await captureLead(publicKey, { name: "Opted In Customer", email: `opted-${stamp}@example.com`, marketingConsent: true });
    check("a voluntary lead capture with marketing consent is accepted", opted.leadResponse.status === 202, opted.leadResponse.status);
    const suppressed = await captureLead(publicKey, { name: "Suppressed Customer", email: `suppressed-${stamp}@example.com`, marketingConsent: true });
    check("a second voluntary lead capture is accepted", suppressed.leadResponse.status === 202, suppressed.leadResponse.status);
    // Simulates this customer having unsubscribed/bounced previously through some other channel
    // (the real path -- the one-click unsubscribe link, or a bounce/complaint webhook -- is
    // covered at the unit/integration level; this just establishes the precondition).
    cfg.sql(`UPDATE customers SET email_suppressed = true WHERE organization_id='${orgId}' AND email='suppressed-${stamp}@example.com'`);

    const campaignName = `Spring sale ${stamp}`;
    await page.goto(`${BASE}/email/campaigns`);
    await page.locator('[data-testid="campaign-form"]').getByLabel("Campaign name").fill(campaignName);
    await page.locator('[data-testid="campaign-form"]').getByLabel("Template").selectOption({ label: templateName });
    await page.locator('[data-testid="campaign-form"]').getByRole("button", { name: /create campaign/i }).click();
    // Creating redirects straight to the new campaign's own detail page, same as templates.
    await page.waitForURL(/\/email\/campaigns\/[0-9a-f-]{36}$/, { timeout: 15000 });
    check("an email campaign can be created as a draft", true);
    await page.reload();

    const campaignId = cfg.sql(`SELECT id FROM email_campaigns WHERE organization_id='${orgId}' AND name='${campaignName}'`);
    await page.locator('[data-testid="campaign-actions"]').getByRole("button", { name: "Start sending" }).click();
    const finalStatus = await eventually(() => {
      const status = cfg.sql(`SELECT status FROM email_campaigns WHERE id='${campaignId}'`);
      return status === "COMPLETED" ? status : null;
    });
    check("starting a campaign eventually completes", finalStatus === "COMPLETED", finalStatus);

    const sentCount = cfg.sql(`SELECT count(*) FROM email_campaign_recipients WHERE campaign_id='${campaignId}' AND status='SENT'`);
    const totalRecipients = cfg.sql(`SELECT count(*) FROM email_campaign_recipients WHERE campaign_id='${campaignId}'`);
    check("the opted-in, non-suppressed customer received the campaign email", sentCount === "1", sentCount);
    // A customer already suppressed when the campaign is queued is excluded during audience
    // resolution itself (never even gets a recipient row) -- the send-time "unsubscribed while
    // queued" race, where a row IS created and then skipped, is covered at the backend unit level
    // (tests/email/test_campaigns.py). Here, the observable proof is simply: only one recipient
    // was ever created for this campaign, not two.
    check("a suppressed contact cannot receive a marketing campaign email", totalRecipients === "1", totalRecipients);

    // ---- 19. email delivery status update ----------------------------------------------------
    const sentEmailId = cfg.sql(`SELECT provider_message_id FROM email_messages WHERE campaign_id='${campaignId}' AND status='SENT' LIMIT 1`);
    check("the sent campaign email has a provider message id", Boolean(sentEmailId));
    const deliveredPayload = { type: "email.delivered", created_at: new Date().toISOString(), data: { email_id: sentEmailId } };
    const deliveredResp = await postEmailWebhook(deliveredPayload);
    check("the signed delivery-status webhook is accepted", deliveredResp.status === 200, deliveredResp.status);
    await eventually(() => cfg.sql(`SELECT status FROM email_messages WHERE provider_message_id='${sentEmailId}'`) === "DELIVERED");
    check(
      "the campaign email's status updates to DELIVERED from a real webhook",
      cfg.sql(`SELECT status FROM email_messages WHERE provider_message_id='${sentEmailId}'`) === "DELIVERED",
    );

    // ---- 20. create an email automation, triggered by a real event ------------------------
    const workflowName = `Lead welcome email ${stamp}`;
    const createWorkflow = await apiPost(`/api/automations`, token, orgId, {
      name: workflowName,
      definition: {
        trigger: { event: "website_lead_captured" },
        conditions: [],
        actions: [{ type: "send_email", subject: "Thanks for reaching out", content: "We received your message.", classification: "TRANSACTIONAL" }],
      },
    });
    const workflow = await createWorkflow.json();
    check("an email-action automation workflow can be created", createWorkflow.status === 201, `${createWorkflow.status} ${JSON.stringify(workflow)}`);
    const activateWorkflow = await apiPost(`/api/automations/${workflow.id}/activate`, token, orgId);
    check("the email-action workflow activates", activateWorkflow.status === 200, activateWorkflow.status);

    const automationLead = await captureLead(publicKey, { name: "Automation Trigger Lead", email: `automation-${stamp}@example.com`, marketingConsent: false });
    check("the automation-triggering lead capture is accepted", automationLead.leadResponse.status === 202, automationLead.leadResponse.status);
    const executionStatus = await eventually(() => {
      const status = cfg.sql(`SELECT e.status FROM automation_executions e JOIN automation_workflows w ON w.id=e.workflow_id WHERE w.name='${workflowName}'`);
      return status === "COMPLETED" || status === "FAILED" ? status : null;
    });
    check("a send_email automation action triggered by a real event completes", executionStatus === "COMPLETED", executionStatus);

    // ---- 21. cross-tenant isolation ----------------------------------------------------------
    const otherContext = await browser.newContext({ viewport: { width: 1000, height: 800 } });
    const otherPage = await otherContext.newPage();
    await signup(otherPage, `other-support-${stamp}@example.com`, `Other Support Org ${stamp}`);
    const { token: otherToken, orgId: otherOrgId } = await orgAndSession(otherContext, `other-support-${stamp}@example.com`);

    const crossConversation = await fetch(`${API}/api/support/conversations/${webConversationId}`, {
      headers: { authorization: `Bearer ${otherToken}`, "x-organization-id": otherOrgId },
    });
    check("another organization gets a 404 for this conversation id", crossConversation.status === 404, crossConversation.status);
    const crossTemplate = await fetch(`${API}/api/email/templates/${templateId}`, {
      headers: { authorization: `Bearer ${otherToken}`, "x-organization-id": otherOrgId },
    });
    check("another organization gets a 404 for this email template id", crossTemplate.status === 404, crossTemplate.status);
    const crossCampaign = await fetch(`${API}/api/email/campaigns/${campaignId}`, {
      headers: { authorization: `Bearer ${otherToken}`, "x-organization-id": otherOrgId },
    });
    check("another organization gets a 404 for this campaign id", crossCampaign.status === 404, crossCampaign.status);

    await otherContext.close();
    await context.close();
  } catch (error) {
    failed++;
    console.log("FAIL  unexpected support E2E error:", error.stack || error);
  } finally {
    await browser.close();
  }
  console.log(failed ? `\n${failed} SUPPORT E2E CHECK(S) FAILED` : "\nALL SUPPORT E2E CHECKS PASSED");
  process.exit(failed ? 1 : 0);
})().catch((error) => {
  console.error("Support E2E crashed:", error);
  process.exit(2);
});
