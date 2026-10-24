const cfg = require("./lib/config");
const http = require("http");
const fs = require("fs");
const path = require("path");

const BASE = cfg.BASE;
const API = cfg.API;
const EMBED = `http://localhost:${cfg.EMBED_PORT}`;
const SHOTS = cfg.ARTIFACTS;
const stamp = Date.now();
const password = "correct-horse-battery";
const MARKER = "zq9371";
const FALLBACK = "I don't have enough information to answer that accurately. Could you share a little more about what you're looking for?";

let failed = 0;
function check(name, ok, extra = "") {
  if (!ok) failed++;
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${extra ? `   [${String(extra).slice(0, 220)}]` : ""}`);
}
const at = (page, p) => new URL(page.url()).pathname === p;
const card = (page, host) => page.locator(`li[data-source-id]:has-text("${host}")`).first();

// A page on a different origin that embeds the widget exactly as a customer would.
const embedServer = http.createServer((req, res) => {
  const key = new URL(req.url, EMBED).searchParams.get("key") || "";
  res.setHeader("content-type", "text/html");
  res.end(`<!doctype html><html><head><meta name="viewport" content="width=device-width, initial-scale=1"><title>Third-party shop</title></head><body><h1>Third-party shop</h1><p>Unrelated page content.</p><script src="${BASE}/widget.js" data-key="${key}" data-api="${API}" async></script></body></html>`);
});

async function signupWithOrg(browser, tag, business) {
  const ctx = await browser.newContext({ viewport: { width: 1100, height: 900 } });
  const page = await ctx.newPage();
  await page.goto(`${BASE}/signup`);
  await page.fill("#email", `chat-${tag}-${stamp}@example.com`);
  await page.fill("#password", password);
  await Promise.all([page.waitForURL(/\/onboarding$/, { timeout: 25000 }), page.click("button[type=submit]")]);
  await page.fill("#name", business);
  await Promise.all([page.waitForURL(/\/dashboard$/, { timeout: 25000 }), page.click("button[type=submit]")]);
  await page.waitForSelector("h1:has-text('Welcome back')", { timeout: 15000 });
  return { ctx, page };
}

const assistantMsgs = (page) => page.locator(".bf-msg.bf-assistant:not(.bf-typing)");
const userMsgs = (page) => page.locator(".bf-msg.bf-user");

async function openChat(page) {
  await page.waitForSelector(".bf-launcher", { timeout: 15000 });
  if (await page.locator(".bf-panel[hidden]").count()) await page.click(".bf-launcher");
  await page.waitForSelector(".bf-panel:not([hidden])", { timeout: 5000 });
}

// Sends a message and waits for the next assistant bubble (or an error box). Returns its text.
async function ask(page, text) {
  const before = await assistantMsgs(page).count();
  const errorsBefore = await page.locator(".bf-error").count();
  await page.fill(".bf-input", text);
  await page.click(".bf-send");
  await page.waitForFunction(
    ([b, e]) => {
      const root = document.querySelector("#bizflow-chat-host").shadowRoot;
      return (
        root.querySelectorAll(".bf-msg.bf-assistant:not(.bf-typing)").length > b ||
        root.querySelectorAll(".bf-error").length > e
      );
    },
    [before, errorsBefore],
    { timeout: 30000 },
  );
  if ((await page.locator(".bf-error").count()) > errorsBefore) {
    return "ERROR:" + (await page.locator(".bf-error").last().textContent());
  }
  return (await assistantMsgs(page).last().textContent()).trim();
}

async function section(name, fn, page) {
  console.log(`\n--- ${name}`);
  try {
    await fn();
  } catch (e) {
    failed++;
    console.log(`FAIL  ${name} threw: ${String(e.message || e).split("\n").slice(0, 12).join("\n      ")}`);
    if (page) await page.screenshot({ caret: "initial", path: path.join(SHOTS, `fail-${name.replace(/\W+/g, "_")}.png`), fullPage: true }).catch(() => {});
  }
}

(async () => {
  await new Promise((r) => embedServer.listen(cfg.EMBED_PORT, r));
  const browser = await cfg.launchBrowser();
  const consoleErrors = [];
  const state = { marker: MARKER };

  const { ctx, page } = await signupWithOrg(browser, "a", "Chat Co");
  page.on("pageerror", (e) => consoleErrors.push("pageerror: " + String(e)));
  page.on("console", (m) => m.type() === "error" && consoleErrors.push("console: " + m.text()));
  const shot = (n) => page.screenshot({ caret: "initial", path: path.join(SHOTS, `${n}.png`), fullPage: true });
  let keyA = "";

  await section("knowledge: crawl example.com", async () => {
    await Promise.all([page.waitForURL(/\/knowledge$/, { timeout: 15000 }), page.click("nav >> text=Knowledge Base")]);
    await page.fill("#url", "https://example.com");
    await page.click("button:has-text('+ Add Website')");
    await card(page, "example.com").waitFor({ timeout: 15000 });
    await card(page, "example.com").locator('[data-status="COMPLETED"]').waitFor({ timeout: 90000 });
    check("example.com crawled to COMPLETED", true);
  }, page);

  await section("website chat page", async () => {
    await Promise.all([page.waitForURL(/\/website-chat$/, { timeout: 15000 }), page.click("nav >> text=Website Chat")]);
    await page.waitForSelector("h1:has-text('Website Chat')", { timeout: 15000 });
    const snippet = await page.textContent("[data-testid=embed-snippet]");
    const m = snippet.match(/data-key="(pk_[A-Za-z0-9_-]+)"/);
    keyA = m ? m[1] : "";
    check("embed snippet has a public key", !!keyA, snippet);
    check("snippet points at widget.js and the API", snippet.includes("/widget.js") && snippet.includes(`data-api="${API}"`));
    const html = await page.content();
    check("no secret-looking values in the page", !/sk-[A-Za-z0-9]{10,}|OPENAI_API_KEY|JWT_SECRET|eyJ[A-Za-z0-9_-]{20,}\./.test(html));
    check("website chat link is current in nav", (await page.locator("nav a[aria-current=page]:visible").textContent()).includes("Website Chat"));
    await shot("c1-website-chat");
  }, page);

  await section("widget: grounded chat on the dashboard preview", async () => {
    await openChat(page);
    check("welcome message shown", (await assistantMsgs(page).first().textContent()).includes("How can I help"));
    await shot("c2-widget-open");

    const r1 = await ask(page, "What is this domain used for in documentation examples?");
    check("grounded answer quotes the crawled page", /Here is what I found/.test(r1) && /documentation examples/i.test(r1), r1);
    check("user bubble was rendered", (await userMsgs(page).count()) === 1);

    const r2 = await ask(page, "Do I need permission to use it?");
    check("short follow-up is still answered from the knowledge base", /Here is what I found/.test(r2), r2);

    const r3 = await ask(page, `What is the refund and return policy for damaged items bought from your store last month ${MARKER}?`);
    check("off-topic question gets the fallback (model not used)", r3 === FALLBACK, r3);
    await shot("c3-conversation");

    const stored = await page.evaluate((k) => sessionStorage.getItem("bizflow_chat_" + k), keyA);
    const conv = JSON.parse(stored);
    check("conversation id + token are kept in sessionStorage", !!conv.id && !!conv.token);
    state.convA = conv;
    state.keyA = keyA;
  }, page);

  await section("widget: history survives a reload; text is never HTML", async () => {
    await page.reload();
    await openChat(page);
    await page.waitForFunction(() => document.querySelector("#bizflow-chat-host").shadowRoot.querySelectorAll(".bf-msg.bf-user").length >= 3, null, { timeout: 15000 });
    check("history restored after reload", (await userMsgs(page).count()) === 3 && (await assistantMsgs(page).count()) >= 4, `${await userMsgs(page).count()} user / ${await assistantMsgs(page).count()} assistant`);

    const payload = `<img src=x onerror="window.__xss=1"><script>window.__xss=1</script> documentation`;
    await ask(page, payload);
    check("script/markup typed by the visitor is displayed as text", (await userMsgs(page).last().textContent()) === payload);
    check("no element was injected into the widget", (await page.locator(".bf-log img, .bf-log script").count()) === 0);
    check("no handler ran", (await page.evaluate(() => window.__xss)) === undefined);
  }, page);

  await section("widget: error states and retry", async () => {
    const msgsBefore = await userMsgs(page).count();
    await page.route("**/api/public/chat/*/messages", (route) => route.abort());
    const r = await ask(page, "documentation examples again please");
    check("network failure shows a friendly error", /couldn't reach/i.test(r), r);
    await page.unroute("**/api/public/chat/*/messages");
    await page.locator(".bf-retry").last().click();
    await page.waitForFunction(() => !document.querySelector("#bizflow-chat-host").shadowRoot.querySelector(".bf-typing"), null, { timeout: 30000 });
    await page.waitForFunction((n) => document.querySelector("#bizflow-chat-host").shadowRoot.querySelectorAll(".bf-msg.bf-user").length >= n, msgsBefore + 1, { timeout: 15000 });
    check("retry succeeded without duplicating the message", (await userMsgs(page).count()) === msgsBefore + 1, `${await userMsgs(page).count()} vs ${msgsBefore + 1}`);

    for (const [status, re, label] of [[429, /too quickly/i, "rate limit"], [503, /unavailable/i, "outage"]]) {
      await page.route("**/api/public/chat/*/messages", (route) =>
        route.fulfill({ status, headers: { "access-control-allow-origin": "*", "content-type": "application/json" }, body: JSON.stringify({ detail: "x" }) }));
      const e = await ask(page, "documentation examples " + status);
      check(`${label} (${status}) shows a friendly error`, re.test(e), e);
      await page.unroute("**/api/public/chat/*/messages");
    }
    await shot("c4-errors");
  }, page);

  await section("widget lifecycle across navigation", async () => {
    await Promise.all([page.waitForURL(/\/knowledge$/, { timeout: 15000 }), page.click("nav >> text=Knowledge Base")]);
    await page.waitForSelector("h1:has-text('Knowledge Base')", { timeout: 15000 });
    await page.waitForTimeout(500);
    check("leaving the page removes the widget (no stray launcher)", (await page.locator("#bizflow-chat-host").count()) === 0);
    await Promise.all([page.waitForURL(/\/website-chat$/, { timeout: 15000 }), page.click("nav >> text=Website Chat")]);
    await page.waitForSelector(".bf-launcher", { timeout: 15000 });
    check("coming back shows exactly one widget", (await page.locator("#bizflow-chat-host").count()) === 1);
    await openChat(page);
    await page.waitForFunction(() => document.querySelector("#bizflow-chat-host").shadowRoot.querySelectorAll(".bf-msg.bf-user").length >= 3, null, { timeout: 15000 });
    check("conversation is still there after navigating away and back", (await userMsgs(page).count()) >= 3);
  }, page);

  await section("settings: disable, origins, enable", async () => {
    const save = async () => {
      await Promise.all([
        page.waitForResponse((r) => r.request().method() === "POST" && new URL(r.url()).pathname === "/website-chat"),
        page.click("button:has-text('Save settings')"),
      ]);
      await page.waitForSelector("form [role=status]:has-text('Settings saved')", { timeout: 10000 });
    };
    const apiStatus = async (origin) =>
      (await fetch(`${API}/api/public/chat/${keyA}/config`, { headers: origin ? { origin } : {} })).status;

    await page.fill("#welcome_message", "Welcome to Chat Co, ask me anything.");
    await page.uncheck("input[name=enabled]");
    await save();
    check("disabled widget: public config is 404", (await apiStatus()) === 404);
    await page.reload();
    check("disabled widget: nothing is rendered", (await page.locator("#bizflow-chat-host").count()) === 0);
    check("disabled notice shown", (await page.textContent("body")).includes("The widget is disabled"));
    await shot("c5-disabled");

    await page.check("input[name=enabled]");
    await page.fill("#allowed_origins", "http://localhost:9999\nhttps://shop.example.com/");
    await save();
    check("origin not in allow-list: 403", (await apiStatus(EMBED)) === 403);
    check("listed origin: 200", (await apiStatus("http://localhost:9999")) === 200);
    check("welcome message was saved", (await (await fetch(`${API}/api/public/chat/${keyA}/config`, { headers: { origin: "http://localhost:9999" } })).json()).welcome_message === "Welcome to Chat Co, ask me anything.");

    await page.reload();
    await page.waitForSelector(".bf-launcher", { timeout: 15000 });
    check("dashboard preview still works when the allow-list does not list the dashboard", true);

    const third = await ctx.newPage();
    await third.goto(`${EMBED}/?key=${keyA}`);
    await third.waitForTimeout(1500);
    check("third-party site not in the allow-list gets no widget", (await third.locator("#bizflow-chat-host").count()) === 0);

    await page.fill("#allowed_origins", EMBED);
    await save();
    await third.reload();
    await third.waitForSelector(".bf-launcher", { timeout: 15000 });
    check("allow-listed third-party origin gets the widget", true);
    await third.close();
    await page.fill("#allowed_origins", "");
    await save();
    check("empty allow-list means any site", (await apiStatus("http://anything.example")) === 200);
  }, page);

  await section("cross-origin embed on a third-party site (real CORS)", async () => {
    const third = await ctx.newPage();
    const errs = [];
    third.on("console", (m) => m.type() === "error" && errs.push(m.text()));
    await third.goto(`${EMBED}/?key=${keyA}`);
    await openChat(third);
    check("custom welcome message appears", (await assistantMsgs(third).first().textContent()).includes("Welcome to Chat Co"));
    const reply = await ask(third, "Tell me about the domain and documentation examples");
    check("third-party site gets a grounded answer", /Here is what I found/.test(reply), reply);
    check("no CORS or console errors on the third-party page", errs.length === 0, errs.join(" | "));
    const cookies = (await ctx.cookies(API)).map((c) => c.name);
    check("the API response did not need or set cookies", !cookies.some((n) => /chat|visitor/i.test(n)));

    const oversize = await third.evaluate(async ([api, key]) => {
      const r = await fetch(`${api}/api/public/chat/${key}/messages`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ conversation_id: "x", content: "a".repeat(30000) }) });
      return { status: r.status, cors: r.headers.get("access-control-allow-origin"), body: await r.text() };
    }, [API, keyA]);
    check("oversized body is refused with 413 and is readable cross-origin (CORS headers present)", oversize.status === 413 && /too large/.test(oversize.body), JSON.stringify(oversize));
    await third.setViewportSize({ width: 375, height: 667 });
    await third.waitForTimeout(300);
    const box = await third.locator(".bf-panel").boundingBox();
    check("mobile: panel fits inside a 375px viewport", box && box.x >= 0 && box.x + box.width <= 375 && box.y >= 0 && box.y + box.height <= 667, JSON.stringify(box));
    check("mobile: page has no horizontal scroll", await third.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth));
    await third.screenshot({ caret: "initial", path: path.join(SHOTS, "c6-mobile.png") });
    await third.close();
  }, page);

  const b = await signupWithOrg(browser, "b", "Other Brand");
  let keyB = "";
  await section("tenant isolation (second organization)", async () => {
    await Promise.all([b.page.waitForURL(/\/website-chat$/, { timeout: 15000 }), b.page.click("nav >> text=Website Chat")]);
    keyB = (await b.page.textContent("[data-testid=embed-snippet]")).match(/data-key="(pk_[A-Za-z0-9_-]+)"/)[1];
    check("each organization has its own public key", keyA && keyB && keyA !== keyB);

    await openChat(b.page);
    const r = await ask(b.page, "What is this domain used for in documentation examples?");
    check("org B (no knowledge) cannot see org A's crawled content", r === FALLBACK, r);

    const { id, token } = state.convA;
    const get = (key, cid, tok) => fetch(`${API}/api/public/chat/${key}/conversations/${cid}`, { headers: tok ? { "x-conversation-token": tok } : {} });
    check("A's conversation with A's token works", (await get(keyA, id, token)).status === 200);
    check("A's conversation through B's key is 404", (await get(keyB, id, token)).status === 404);
    check("A's conversation without a token is 404", (await get(keyA, id, null)).status === 404);
    check("A's conversation with a wrong token is 404", (await get(keyA, id, "x".repeat(43))).status === 404);
    const post = await fetch(`${API}/api/public/chat/${keyB}/messages`, { method: "POST", headers: { "content-type": "application/json", "x-conversation-token": token }, body: JSON.stringify({ conversation_id: id, content: "hello there" }) });
    check("posting into A's conversation through B's key is 404", post.status === 404);
    const body = await (await get(keyA, id, token)).text();
    check("history payload exposes no internal fields", !/organization|sources|prompt|embedding|score|meta|grounded/i.test(body), body.slice(0, 160));
    check("an unknown key is 404", (await fetch(`${API}/api/public/chat/pk_${"a".repeat(32)}/config`)).status === 404);
    check("a malformed key is 404", (await fetch(`${API}/api/public/chat/not-a-key/config`)).status === 404);
    const organizationInjected = await fetch(`${API}/api/public/chat/${keyB}/conversations`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ organization_id: "00000000-0000-0000-0000-000000000000" }) });
    check("browser-supplied organization_id has no effect", organizationInjected.status === 201 || organizationInjected.status === 422, organizationInjected.status);
  }, b.page);

  await section("rate limit (real, via the API)", async () => {
    const created = await (await fetch(`${API}/api/public/chat/${keyB}/conversations`, { method: "POST", headers: { "content-type": "application/json" }, body: "{}" })).json();
    const statuses = [];
    for (let i = 0; i < 30; i++) {
      const r = await fetch(`${API}/api/public/chat/${keyB}/messages`, { method: "POST", headers: { "content-type": "application/json", "x-conversation-token": created.token }, body: JSON.stringify({ conversation_id: created.conversation_id, content: `hello number ${i}` }) });
      statuses.push(r.status);
      if (r.status === 429) {
        check("429 carries Retry-After", !!r.headers.get("retry-after"));
        break;
      }
    }
    check("flooding is rate limited", statuses.includes(429), statuses.join(","));
    check("requests before the limit succeeded", statuses[0] === 200, statuses.slice(0, 3).join(","));
    await openChat(b.page);
    const rl = await ask(b.page, "one more question about documentation");
    check("widget shows the friendly rate-limit message", /too quickly/i.test(rl), rl);
  }, b.page);

  console.log("\nconsole/page errors on the dashboard page:");
  const interesting = consoleErrors.filter((e) => !/Failed to load resource|ERR_FAILED|status of (429|503)/i.test(e));
  console.log(interesting.length ? interesting.join("\n") : "  none (expected network failures filtered)");
  check("no unexpected browser errors", interesting.length === 0);

  fs.writeFileSync(path.join(SHOTS, "state.json"), JSON.stringify({ ...state, keyB }));
  await browser.close();
  embedServer.close();
  console.log(`\n${failed === 0 ? "ALL CHECKS PASSED" : failed + " CHECK(S) FAILED"}`);
  process.exit(failed === 0 ? 0 : 1);
})();
