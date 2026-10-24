const cfg = require("./lib/config");
const crypto = require("crypto");
const { execFileSync } = require("child_process");
const fs = require("fs");
const path = require("path");

const BASE = cfg.BASE;
const API = cfg.API;
const FAKE = cfg.FAKE;
const SHOTS = cfg.ARTIFACTS;
const stamp = Date.now();
const password = "correct-horse-battery";
const SHOP = cfg.SHOP;
const CLIENT_SECRET = cfg.CLIENT_SECRET;
const PAGE = `${BASE}/integrations/shopify`;

let failed = 0;
function check(name, ok, extra = "") {
  if (!ok) failed++;
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${extra ? `   [${String(extra).slice(0, 240)}]` : ""}`);
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const at = (page, p) => new URL(page.url()).pathname === p;

const sql = cfg.sql;
async function control(action, body = {}) {
  const r = await fetch(`${FAKE}/__control/${action}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  return r.json();
}
async function fakeState() {
  return (await fetch(`${FAKE}/__control/state`)).json();
}
function signWebhook(body) {
  return crypto.createHmac("sha256", CLIENT_SECRET).update(body).digest("base64");
}
async function deliver(topic, payload, { id, secret = CLIENT_SECRET, shop = SHOP } = {}) {
  const body = JSON.stringify(payload);
  const sig = crypto.createHmac("sha256", secret).update(body).digest("base64");
  const r = await fetch(`${API}/api/webhooks/shopify`, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "x-shopify-topic": topic,
      "x-shopify-shop-domain": shop,
      "x-shopify-webhook-id": id,
      "x-shopify-hmac-sha256": sig,
    },
    body,
  });
  return { status: r.status, body: await r.text() };
}

async function apiJson(pathname, token, opts = {}) {
  const r = await fetch(`${API}${pathname}`, {
    ...opts,
    headers: { authorization: `Bearer ${token}`, "content-type": "application/json", ...(opts.headers || {}) },
  });
  let json = null;
  try { json = await r.json(); } catch {}
  return { status: r.status, json };
}

// Everything the browser ever receives is recorded, to prove no secret is among it.
const seenText = [];
function record(page) {
  page.on("response", async (res) => {
    try {
      const ct = res.headers()["content-type"] || "";
      if (/json|html|text|javascript|x-component/.test(ct)) seenText.push(await res.text());
    } catch {}
  });
}

async function newUser(browser, tag, business) {
  const ctx = await browser.newContext({ viewport: { width: 1100, height: 1000 } });
  const page = await ctx.newPage();
  record(page);
  await page.goto(`${BASE}/signup`);
  await page.fill("#email", `shop-${tag}-${stamp}@example.com`);
  await page.fill("#password", password);
  await Promise.all([page.waitForURL(/\/onboarding$/, { timeout: 25000 }), page.click("button[type=submit]")]);
  await page.fill("#name", business);
  await Promise.all([page.waitForURL(/\/dashboard$/, { timeout: 25000 }), page.click("button[type=submit]")]);
  await page.waitForSelector("h1:has-text('Welcome back')", { timeout: 15000 });
  return { ctx, page, email: `shop-${tag}-${stamp}@example.com` };
}

async function login(browser, email) {
  const ctx = await browser.newContext({ viewport: { width: 1100, height: 1000 } });
  const page = await ctx.newPage();
  record(page);
  await page.goto(`${BASE}/login`);
  await page.fill("#email", email);
  await page.fill("#password", password);
  await Promise.all([page.waitForURL(/\/dashboard$/, { timeout: 25000 }), page.click("button[type=submit]")]);
  return { ctx, page };
}

async function apiToken(email) {
  const r = await fetch(`${API}/api/auth/login`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
  return (await r.json()).access_token;
}

// React resets an uncontrolled form once its action settles; fill again until the value sticks.
async function setShop(page, value) {
  for (let i = 0; i < 6; i++) {
    await page.fill("#shop", value);
    await sleep(350);
    if ((await page.inputValue("#shop")) === value) return;
  }
  throw new Error("could not set the shop field");
}

const visible = (page, sel) => page.locator(`${sel}:visible`);
const text = async (page, sel) => ((await visible(page, sel).first().textContent()) || "").trim();
const count = async (page, key) => Number(await text(page, `[data-count="${key}"]`));
const stage = (page, name) => visible(page, `li[data-stage="${name}"]`);

async function waitIdle(page, timeout = 90000) {
  await page.waitForFunction(
    () => {
      const badge = document.querySelector('[data-testid="shopify-store"] [data-status]');
      return badge && badge.getAttribute("data-status") === "CONNECTED";
    },
    null,
    { timeout },
  );
}

(async () => {
  const browser = await cfg.launchBrowser();
  try {
    const initial = await fakeState();
    check("fake Shopify is up", initial.mode === "approve");

    // ---- two businesses, and a plain member of the first ---------------------------------------------
    const A = await newUser(browser, "a", "Alpha Soap");
    const B = await newUser(browser, "b", "Beta Candles");
    const pageA = A.page;
    const pageB = B.page;

    // =============== A: before connecting ======================================================
    await pageA.click('nav[aria-label="Main"] >> text=Shopify');
    await pageA.waitForURL(/\/integrations\/shopify$/);
    await pageA.waitForSelector("h1:has-text('Shopify')");
    check("nav link opens /integrations/shopify", at(pageA, "/integrations/shopify"));
    check("not connected: the connect form is shown", await visible(pageA, "#shop").count() === 1);
    check("no store card before connecting", (await pageA.locator('[data-testid="shopify-store"]').count()) === 0);
    await pageA.screenshot({ caret: "initial", path: path.join(SHOTS, "s1-connect.png") });

    // ---- validation errors, nothing leaves the app -------------------------------------------------
    const authBefore = (await fakeState()).authorize_calls;
    for (const bad of ["evil.com", "localhost", "127.0.0.1", "https://x.myshopify.com.evil.com", "a b.myshopify.com", "x.myshopify.com/../admin"]) {
      await setShop(pageA, bad);
      await pageA.click("button[type=submit]");
      await pageA.waitForSelector("#shop-error", { timeout: 10000 });
      check(`rejected shop address "${bad}"`, /store address/i.test(await text(pageA, "#shop-error")) && at(pageA, "/integrations/shopify"));
    }
    check("no OAuth redirect was started for any bad address", (await fakeState()).authorize_calls === authBefore);

    // ---- the merchant declines in Shopify ---------------------------------------------------------------
    await control("mode", { mode: "decline" });
    await setShop(pageA, SHOP);
    await Promise.all([pageA.waitForURL(/\/integrations\/shopify\?error=access_denied$/, { timeout: 30000 }), pageA.click("button[type=submit]")]);
    check("declining in Shopify shows a calm message and changes nothing",
      /cancelled in Shopify/i.test(await text(pageA, "main [role=alert]")) && (await pageA.locator('[data-testid="shopify-store"]').count()) === 0);
    check("no connection row after declining", sql(`select count(*) from shopify_connections where status <> 'DISCONNECTED'`) === "0");

    // ---- a forged callback (wrong signature) ------------------------------------------------------------
    await control("mode", { mode: "forge" });
    await setShop(pageA, SHOP);
    await Promise.all([pageA.waitForURL(/\/integrations\/shopify\?error=invalid_signature$/, { timeout: 30000 }), pageA.click("button[type=submit]")]);
    check("a callback with a bad signature is refused", /could not be verified/i.test(await text(pageA, "main [role=alert]")));
    check("and nothing was exchanged for a token", (await fakeState()).token_requests === 0);

    // ---- an arbitrary message in ?error= cannot appear -----------------------------------------------------
    await pageA.goto(`${PAGE}?error=${encodeURIComponent("Your account is locked, call 555-0100")}`);
    check("unknown error codes show only generic text", /could not be completed/i.test(await text(pageA, "main [role=alert]")) && !(await pageA.innerText("main")).includes("555-0100"));

    // ---- connect for real, with a slow Shopify so progress is observable ----------------------------------------
    await control("mode", { mode: "approve" });
    await control("delay", { seconds: 0.7 });
    await pageA.goto(PAGE);
    await setShop(pageA, `  ${SHOP.toUpperCase()}  `); // input is normalized
    await Promise.all([pageA.waitForURL(/\/integrations\/shopify\?connected=1$/, { timeout: 40000 }), pageA.click("button[type=submit]")]);
    check("OAuth round trip ends on the page with a success notice", /store is connected/i.test(await text(pageA, "main [role=status]")));
    check("shows the store domain", (await text(pageA, '[data-field="domain"]')) === SHOP);
    check("shows the shop name from Shopify", /E2E Soap Shop/.test(await text(pageA, '[data-testid="shopify-store"]')));
    check("shows a connected date", /UTC/.test(await text(pageA, '[data-field="installed"]')));
    check("last sync is 'never' before the first import ends", (await text(pageA, '[data-field="last-sync"]')) === "never" || /UTC/.test(await text(pageA, '[data-field="last-sync"]')));
    check("status shows Syncing while the first import runs", (await pageA.locator('[data-testid="shopify-store"] [data-status="SYNCING"]:visible').count()) === 1);
    check("the Sync button is disabled during a sync", await pageA.locator('button:has-text("Sync in progress"):visible').isDisabled());
    await pageA.screenshot({ caret: "initial", path: path.join(SHOTS, "s2-syncing.png") });

    // progress is live: counts rise without reloading
    const first = await count(pageA, "products");
    let rose = false;
    for (let i = 0; i < 40 && !rose; i++) {
      await sleep(1000);
      rose = (await count(pageA, "products")) > first || (await count(pageA, "customers")) > 0;
    }
    check("counts update on their own while syncing (auto-refresh)", rose);

    await waitIdle(pageA);
    check("sync finishes: status Connected", (await pageA.locator('[data-testid="shopify-store"] [data-status="CONNECTED"]:visible').count()) === 1);
    check("products imported", (await count(pageA, "products")) === 40, await count(pageA, "products"));
    check("variants imported", (await count(pageA, "variants")) === 80, await count(pageA, "variants"));
    check("customers imported", (await count(pageA, "customers")) === 25, await count(pageA, "customers"));
    check("orders imported", (await count(pageA, "orders")) === 30, await count(pageA, "orders"));
    for (const s of ["PRODUCTS", "CUSTOMERS", "ORDERS"]) {
      check(`stage ${s} completed`, (await stage(pageA, s).locator('[data-status="COMPLETED"]').count()) === 1);
    }
    check("last sync date is set", /UTC/.test(await text(pageA, '[data-field="last-sync"]')));
    check("webhooks were registered", /Live updates are on/.test(await text(pageA, '[data-testid="shopify-store"]')));
    check("granted scopes are listed (names only)", (await text(pageA, '[data-testid="shopify-scopes"]')) === "read_customers, read_orders, read_products");
    await pageA.screenshot({ caret: "initial", path: path.join(SHOTS, "s3-synced.png"), fullPage: true });

    const orgId = sql(`select organization_id from shopify_connections where status <> 'DISCONNECTED' limit 1`);
    check("database holds the imported rows for this organization only",
      sql(`select count(*) from products where organization_id='${orgId}'`) === "40" &&
      sql(`select count(*) from products`) === "40");

    // ---- credentials at rest -----------------------------------------------------------------------------------
    const stored = sql(`select access_token_encrypted from shopify_connections where organization_id='${orgId}'`);
    const tokens = (await fakeState()).tokens;
    check("the stored token is ciphertext (bf1:...), not the token", stored.startsWith("bf1:") && tokens.every((t) => !stored.includes(t)));

    // ---- Sync now ------------------------------------------------------------------------------------------------
    await control("delay", { seconds: 0.3 });
    await sleep(3500); // past the minimum interval between syncs
    await control("seed", { products: 45, customers: 25, orders: 30 });
    await pageA.click('button:has-text("Sync now")');
    await pageA.waitForSelector('[data-testid="shopify-store"] [data-status="SYNCING"]', { timeout: 15000 });
    check("Sync now starts a sync", true);
    const tokenA = await apiToken(A.email);
    const second = await apiJson("/api/integrations/shopify/sync", tokenA, { method: "POST" });
    check("asking for another sync while one runs is refused (409, already in progress)",
      second.status === 409 && /already in progress/i.test(second.json.detail), [second.status, second.json?.detail]);
    await waitIdle(pageA);
    check("the manual sync picked up new data (45 products) without duplicating the rest",
      (await count(pageA, "products")) === 45 && (await count(pageA, "customers")) === 25 && (await count(pageA, "orders")) === 30);
    check("only one sync ran for the two requests", sql(`select count(*) from products`) === "45");

    // ---- webhooks --------------------------------------------------------------------------------------------------
    await control("rename_product", { id: 3, title: "Renamed by merchant" });
    const hook = await deliver("products/update", { id: 3, title: "ignored payload" }, { id: `wh-${stamp}-1` });
    check("a correctly signed webhook gets an immediate 200 accepted", hook.status === 200 && /accepted/.test(hook.body), hook.body);
    const dup = await deliver("products/update", { id: 3 }, { id: `wh-${stamp}-1` });
    check("the same delivery id again is recognised as a duplicate", dup.status === 200 && /duplicate/.test(dup.body), dup.body);
    const forged = await deliver("products/update", { id: 3 }, { id: `wh-${stamp}-2`, secret: "wrong-secret" });
    check("a webhook with a wrong signature is refused (401)", forged.status === 401, forged.status);
    const other = await deliver("products/update", { id: 3 }, { id: `wh-${stamp}-3`, shop: "someone-else.myshopify.com" });
    check("a signed webhook for an unknown store is acknowledged and ignored", other.status === 200 && /ignored/.test(other.body), other.body);
    let title = "";
    for (let i = 0; i < 30 && title !== "Renamed by merchant"; i++) {
      await sleep(500);
      title = sql(`select title from products where organization_id='${orgId}' and shopify_product_id=3`);
    }
    check("the worker applied the webhook (title updated from Shopify, not from the payload)", title === "Renamed by merchant", title);
    check("exactly one event row for the duplicated delivery",
      sql(`select count(*) from shopify_webhook_events where webhook_id='wh-${stamp}-1'`) === "1");
    check("the event ledger stores no payload or personal data",
      sql(`select count(*) from information_schema.columns where table_name='shopify_webhook_events' and column_name in ('payload','body','email','data')`) === "0");
    await control("delay", { seconds: 0 });

    // ---- a member of the organization can look but not touch ---------------------------------------------------------------
    const memberEmail = `shop-m-${stamp}@example.com`;
    await fetch(`${API}/api/auth/register`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ email: memberEmail, password }) });
    const orgRow = await apiJson("/api/organizations/current", tokenA);
    const added = await apiJson(`/api/organizations/${orgRow.json.id}/members`, tokenA, { method: "POST", body: JSON.stringify({ email: memberEmail, role: "MEMBER" }) });
    check("member added to Alpha", added.status === 201);
    const M = await login(browser, memberEmail);
    await M.page.goto(PAGE);
    await M.page.waitForSelector('[data-testid="shopify-store"]');
    check("a member sees the store and its counts", (await count(M.page, "products")) === 45);
    check("a member sees no Sync or Disconnect buttons", (await M.page.locator('button:has-text("Sync now"), button:has-text("Disconnect")').count()) === 0);
    check("a member is told why", /Only owners and admins/i.test(await text(M.page, '[data-testid="shopify-store"]')));
    const tokenM = await apiToken(memberEmail);
    const memberSync = await apiJson("/api/integrations/shopify/sync", tokenM, { method: "POST" });
    const memberDisc = await apiJson("/api/integrations/shopify/disconnect", tokenM, { method: "POST" });
    const memberConn = await apiJson("/api/integrations/shopify/connect", tokenM, { method: "POST", body: JSON.stringify({ shop: SHOP }) });
    check("the API refuses a member's sync / disconnect / connect (403)", [memberSync.status, memberDisc.status, memberConn.status].every((s) => s === 403), [memberSync.status, memberDisc.status, memberConn.status]);
    await M.ctx.close();

    // =============== B: another business ======================================================================
    await pageB.goto(PAGE);
    await pageB.waitForSelector("#shop");
    check("tenant B sees no store, data or counts of tenant A",
      (await pageB.locator('[data-testid="shopify-store"]').count()) === 0 && !(await pageB.content()).includes("E2E Soap Shop"));
    await setShop(pageB, SHOP);
    await pageB.click("button[type=submit]");
    await pageB.waitForSelector("main [role=alert]", { timeout: 20000 });
    check("tenant B cannot connect the store tenant A already owns", /already connected to another BizFlow account/i.test(await text(pageB, "main [role=alert]")));
    const tokenB = await apiToken(B.email);
    const peek = await apiJson("/api/integrations/shopify", tokenB, { headers: { "X-Organization-ID": orgRow.json.id } });
    check("naming Alpha's organization id gives B nothing (403/404)", [403, 404].includes(peek.status), peek.status);
    const bStatus = await apiJson("/api/integrations/shopify", tokenB);
    check("B's own status has no connection and zero counts", bStatus.json.connection === null && bStatus.json.counts.products === 0);

    // =============== disconnect (two-step), data removal ============================================================
    await pageA.goto(PAGE);
    await pageA.waitForSelector('[data-testid="shopify-store"]');
    await pageA.click('button:has-text("Disconnect")');
    await pageA.waitForSelector("text=Disconnect this store?");
    check("disconnect asks first and explains the data removal", /remove the products, customers\s+and orders it imported/i.test((await pageA.textContent("body")).replace(/\s+/g, " ")) || /remove the products, customers and orders it imported/i.test((await pageA.textContent("body")).replace(/\s+/g, " ")));
    await pageA.screenshot({ caret: "initial", path: path.join(SHOTS, "s4-disconnect-confirm.png"), fullPage: true });
    await pageA.click('button:has-text("Cancel")');
    check("Cancel closes the warning and keeps the store", (await pageA.locator("text=Disconnect this store?").count()) === 0 && (await count(pageA, "products")) === 45);
    await pageA.click('button:has-text("Disconnect")');
    await Promise.all([pageA.waitForURL(/\/integrations\/shopify\?disconnected=1$/, { timeout: 30000 }), pageA.click('button:has-text("Yes, disconnect")')]);
    check("after disconnecting the page says so and offers to connect again", /disconnected/i.test(await text(pageA, "main [role=status]")) && (await visible(pageA, "#shop").count()) === 1);
    check("the imported data is gone", sql(`select count(*) from products where organization_id='${orgId}'`) === "0" && sql(`select count(*) from orders where organization_id='${orgId}'`) === "0" && sql(`select count(*) from customers where organization_id='${orgId}'`) === "0");
    check("the stored credentials are gone", sql(`select (access_token_encrypted is null and refresh_token_encrypted is null)::text from shopify_connections where organization_id='${orgId}'`) === "true");
    check("BizFlow's webhook subscriptions were removed at Shopify", (await fakeState()).subscriptions === 0);
    check("sync progress rows are gone", sql(`select count(*) from shopify_sync_states`) === "0");
    check("tenant B's data (none) and the store are untouched / unaffected", sql(`select count(*) from products`) === "0");
    await pageA.screenshot({ caret: "initial", path: path.join(SHOTS, "s5-disconnected.png") });

    // ---- the OAuth state is bound to the user who started it ------------------------------------------------------------------
    // Start A's flow through the API and read the signed callback URL Shopify would redirect to.
    const startedByA = await apiJson("/api/integrations/shopify/connect", tokenA, {
      method: "POST",
      body: JSON.stringify({ shop: SHOP }),
    });
    const authorize = await fetch(startedByA.json.authorization_url, { redirect: "manual" });
    const captured = authorize.headers.get("location");
    check("captured the signed callback URL of A's flow", !!captured, captured);
    await pageB.goto(captured);
    await pageB.waitForURL(/\?error=/, { timeout: 20000 });
    check("tenant B cannot complete A's authorization (state belongs to another session)", /error=invalid_state/.test(pageB.url()) && /could not be verified/i.test(await text(pageB, "main [role=alert]")), pageB.url());
    check("and nothing was connected by it", sql(`select count(*) from shopify_connections where status <> 'DISCONNECTED'`) === "0");
    await Promise.all([pageA.waitForURL(/\?connected=1$/, { timeout: 40000 }), pageA.goto(captured)]);
    await waitIdle(pageA);
    check("A completes their own authorization with the same URL", (await count(pageA, "products")) === 45);
    await pageA.goto(captured);
    await pageA.waitForURL(/\?error=/, { timeout: 20000 });
    check("replaying a used callback is refused (single-use state)", /error=invalid_state/.test(pageA.url()), pageA.url());
    check("the replay did not create a second connection or token exchange", sql(`select count(*) from shopify_connections`) === "1");
    await pageA.goto(PAGE);
    await pageA.click('button:has-text("Disconnect")');
    await Promise.all([pageA.waitForURL(/disconnected=1$/, { timeout: 30000 }), pageA.click('button:has-text("Yes, disconnect")')]);

    // ---- the freed store can be connected by the other tenant, then back by A ------------------------------------------------------------
    await control("delay", { seconds: 0 });
    await pageB.goto(PAGE);
    await setShop(pageB, SHOP);
    await Promise.all([pageB.waitForURL(/\?connected=1$/, { timeout: 40000 }), pageB.click("button[type=submit]")]);
    await waitIdle(pageB);
    check("after A disconnects, B can connect that store", (await count(pageB, "products")) === 45);
    check("and A still sees nothing of it", await (async () => { await pageA.goto(PAGE); await pageA.waitForSelector("#shop"); return (await pageA.locator('[data-testid="shopify-store"]').count()) === 0; })());
    await pageB.goto(PAGE);
    await pageB.click('button:has-text("Disconnect")');
    await Promise.all([pageB.waitForURL(/disconnected=1$/, { timeout: 30000 }), pageB.click('button:has-text("Yes, disconnect")')]);

    // ---- A reconnects: everything comes back ----------------------------------------------------------------------------------------------
    await pageA.goto(PAGE);
    await setShop(pageA, SHOP);
    await Promise.all([pageA.waitForURL(/\?connected=1$/, { timeout: 40000 }), pageA.click("button[type=submit]")]);
    await waitIdle(pageA);
    check("reconnecting re-imports everything", (await count(pageA, "products")) === 45 && (await count(pageA, "orders")) === 30);
    check("no duplicate rows after the second import", sql(`select count(*) from products`) === "45" && sql(`select count(*) from (select 1 from products group by organization_id, shopify_product_id having count(*) > 1) d`) === "0");

    // ---- revoked access at Shopify -> needs attention + reconnect ------------------------------------------------------------------
    await control("revoke");
    await sleep(3500);
    await pageA.reload();
    await pageA.waitForSelector('[data-testid="shopify-store"] [data-status]'); // the card streams in after "load"
    // Shopify's rejection can be discovered by the click below OR, first, by the worker's webhook
    // subscription audit (it calls Shopify shortly after a connect). Both must end in "Needs attention".
    const flaggedAlready = (await pageA.locator('[data-testid="shopify-store"] [data-status="ERROR"]:visible').count()) === 1;
    if (!flaggedAlready) await pageA.click('button:has-text("Sync now")');
    let needs = false;
    for (let i = 0; i < 30 && !needs; i++) {
      await sleep(1000);
      await pageA.reload();
      await pageA.waitForSelector('[data-testid="shopify-store"] [data-status]');
      needs = (await pageA.locator('[data-testid="shopify-store"] [data-status="ERROR"]:visible').count()) === 1;
    }
    check("when Shopify rejects the saved access the store is flagged 'Needs attention'", needs);
    if (needs) {
      check("with a plain-language reason", /reconnect/i.test(await text(pageA, '[data-testid="shopify-store"] [role=alert]')));
      check("and a Reconnect form", (await pageA.locator('button:has-text("Reconnect Shopify")').count()) === 1);
      check("Sync now is disabled until the store is reconnected", await pageA.locator('button:has-text("Sync now")').isDisabled());
      await pageA.screenshot({ caret: "initial", path: path.join(SHOTS, "s6-needs-attention.png"), fullPage: true });
      await Promise.all([pageA.waitForURL(/\?connected=1$/, { timeout: 40000 }), pageA.click('button:has-text("Reconnect Shopify")')]);
      await waitIdle(pageA);
      check("reconnecting restores the store", (await pageA.locator('[data-testid="shopify-store"] [data-status="CONNECTED"]:visible').count()) === 1);
    }

    // ---- nothing secret ever reached the browser ----------------------------------------------------------------------------------------------
    const secrets = [CLIENT_SECRET, ...(await fakeState()).tokens, "bf1:", "shpat_", "shprt_", "access_token", "private-example.test", "+1555000"];
    const joined = seenText.join("\n");
    const leaks = secrets.filter((s) => joined.includes(s));
    check("no token, ciphertext, client secret or customer data in anything the browser received", leaks.length === 0, leaks.join(","));
    const cookies = await A.ctx.cookies();
    check("the session cookie is HttpOnly", cookies.find((c) => c.name === "bizflow_session")?.httpOnly === true);
    const storage = await pageA.evaluate(() => JSON.stringify({ ...localStorage }) + JSON.stringify({ ...sessionStorage }));
    check("nothing in browser storage", storage === "{}{}");

    // ---- a signed-out visitor ---------------------------------------------------------------------------------------------------------------------------
    const anon = await browser.newContext();
    const ap = await anon.newPage();
    await ap.goto(PAGE);
    check("signed-out users are sent to /login", at(ap, "/login"));
    const cb = await ap.goto(`${PAGE}/callback?code=x&shop=${SHOP}`);
    check("the callback route also requires a session", at(ap, "/login"));
    const anonApi = await fetch(`${API}/api/integrations/shopify`);
    check("the API refuses anonymous status requests", anonApi.status === 401);
    await anon.close();

    // ---- mobile layout ------------------------------------------------------------------------------------------------------------------------------------
    await pageA.setViewportSize({ width: 390, height: 844 });
    await pageA.goto(PAGE);
    await pageA.waitForSelector('[data-testid="shopify-store"]');
    const overflow = await pageA.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1);
    check("mobile layout has no horizontal scroll", !overflow);
    await pageA.screenshot({ caret: "initial", path: path.join(SHOTS, "s7-mobile.png"), fullPage: true });
  } catch (e) {
    failed++;
    console.log("FAIL  unexpected error:", e.stack || e);
  } finally {
    await browser.close();
  }
  console.log(failed ? `\n${failed} CHECK(S) FAILED` : "\nALL CHECKS PASSED");
  process.exit(failed ? 1 : 0);
})();
