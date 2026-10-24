const cfg = require("./lib/config");
const path = require("path");

const BASE = cfg.BASE;
const SHOTS = cfg.ARTIFACTS;
const stamp = Date.now();
const password = "correct-horse-battery";

let failed = 0;
function check(name, ok, extra = "") {
  if (!ok) failed++;
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${extra ? `   [${extra}]` : ""}`);
}
const at = (page, p) => new URL(page.url()).pathname === p;
const card = (page, host) => page.locator(`li[data-source-id]:has-text("${host}")`).first();

async function signupWithOrg(browser, tag, business) {
  const ctx = await browser.newContext({ viewport: { width: 1100, height: 900 } });
  const page = await ctx.newPage();
  await page.goto(`${BASE}/signup`);
  await page.fill("#email", `kb-${tag}-${stamp}@example.com`);
  await page.fill("#password", password);
  await Promise.all([page.waitForURL(/\/onboarding$/, { timeout: 25000 }), page.click("button[type=submit]")]);
  await page.fill("#name", business);
  await Promise.all([page.waitForURL(/\/dashboard$/, { timeout: 25000 }), page.click("button[type=submit]")]);
  await page.waitForSelector("h1:has-text('Welcome back')", { timeout: 15000 });
  return { ctx, page };
}

(async () => {
  const browser = await cfg.launchBrowser();
  const errors = [];
  const { ctx, page } = await signupWithOrg(browser, "a", "Knowledge Co");
  page.on("pageerror", (e) => errors.push(String(e)));
  const shot = (n) => page.screenshot({ caret: "initial", path: path.join(SHOTS, `${n}.png`), fullPage: true });

  // --- navigation to the knowledge base
  await Promise.all([page.waitForURL(/\/knowledge$/, { timeout: 15000 }), page.click("nav >> text=Knowledge Base")]);
  await page.waitForSelector("h1:has-text('Knowledge Base')", { timeout: 15000 });
  check("dashboard links to /knowledge", at(page, "/knowledge"));
  check("empty state is shown", (await page.textContent("body")).includes("No sources yet"));
  check("'+ Add Website' button exists", (await page.locator("button:has-text('+ Add Website')").count()) === 1);
  await shot("k1-empty");

  // --- dangerous addresses are refused with a readable message and create nothing
  for (const [bad, expect] of [
    ["http://localhost:8000", /not a public website/i],
    ["http://169.254.169.254/latest/meta-data/", /not a public website/i],
    ["ftp://example.com", /http/i],
    ["https://example.com:8080", /standard web ports/i],
  ]) {
    await page.fill("#url", bad);
    // wait for THIS submission's server response so we never read the previous error's text
    await Promise.all([
      page.waitForResponse((r) => r.request().method() === "POST" && new URL(r.url()).pathname === "/knowledge"),
      page.click("button:has-text('+ Add Website')"),
    ]);
    await page.waitForTimeout(400);
    const msg = await page.textContent("form [role=alert]");
    check(`rejects ${bad}`, expect.test(msg), msg.trim());
    check(`  ...and no source was created for ${bad}`, (await page.locator("li[data-source-id]").count()) === 0);
  }
  await shot("k2-rejected");

  // --- the main flow from the brief: add https://example.com
  await page.fill("#url", "https://example.com");
  await page.click("button:has-text('+ Add Website')");
  await page.waitForSelector("form [role=status]", { timeout: 15000 });
  check("success message after adding", /queued/i.test(await page.textContent("form [role=status]")));
  await card(page, "example.com").waitFor({ timeout: 15000 });
  check("source card appears", (await card(page, "example.com").count()) === 1);
  const firstStatus = await card(page, "example.com").locator("[data-status]").first().getAttribute("data-status");
  check("status starts as PENDING or CRAWLING", ["PENDING", "CRAWLING"].includes(firstStatus), firstStatus);
  await shot("k3-crawling");

  await card(page, "example.com").locator('[data-status="COMPLETED"]').waitFor({ timeout: 90000 });
  check("status became COMPLETED without a manual reload (auto-refresh)", true);
  const pages1 = await card(page, "example.com").locator('[data-field="pages"]').textContent();
  check("shows page count", Number(pages1) >= 1, `Pages: ${pages1}`);
  check("shows last crawled time", /\d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC/.test(await card(page, "example.com").textContent()));
  await shot("k4-completed");

  // --- open the source and inspect discovered pages
  await Promise.all([
    page.waitForURL(/\/knowledge\/[0-9a-f-]{36}$/, { timeout: 15000 }),
    card(page, "example.com").locator("a:has-text('Open')").click(),
  ]);
  await page.waitForSelector("h2:has-text('Discovered pages')", { timeout: 15000 });
  const docItems = page.locator("li[data-document-id]");
  check("documents are listed", (await docItems.count()) >= 1, `${await docItems.count()} pages`);
  const docText = await docItems.first().textContent();
  check("document shows title, chunks and a text preview", /Example Domain/.test(docText) && /chunk/.test(docText) && /documentation examples/i.test(docText));
  await shot("k5-source");

  await Promise.all([
    page.waitForURL(/\/documents\/[0-9a-f-]{36}$/, { timeout: 15000 }),
    docItems.first().locator("a").first().click(),
  ]);
  await page.waitForSelector("pre", { timeout: 15000 });
  const content = await page.textContent("pre");
  check("document detail shows the cleaned text", /This domain is for use in documentation examples/.test(content));
  check("no raw HTML in stored/displayed text", !/<\/?(html|body|div|script|p)\b/i.test(content));
  await shot("k6-document");
  await page.goBack(); await page.goBack();
  await page.waitForSelector("h1:has-text('Knowledge Base')", { timeout: 15000 });

  // --- re-crawl
  await card(page, "example.com").locator("button:has-text('Re-crawl')").click();
  await card(page, "example.com").locator('[data-status="PENDING"], [data-status="CRAWLING"]').first().waitFor({ timeout: 15000 });
  check("re-crawl moves the source back into PENDING/CRAWLING", true);
  await card(page, "example.com").locator('[data-status="COMPLETED"]').waitFor({ timeout: 90000 });
  check("re-crawl completes again", true);
  check("page count is stable after a re-crawl (no duplicates)", (await card(page, "example.com").locator('[data-field="pages"]').textContent()) === pages1);

  // --- a multi-page real site, limited to 8 pages by the worker
  await page.fill("#url", "https://quotes.toscrape.com");
  await page.click("button:has-text('+ Add Website')");
  await card(page, "quotes.toscrape.com").waitFor({ timeout: 15000 });
  await card(page, "quotes.toscrape.com").locator('[data-status="COMPLETED"], [data-status="FAILED"]').first().waitFor({ timeout: 120000 });
  const qStatus = await card(page, "quotes.toscrape.com").locator("[data-status]").first().getAttribute("data-status");
  const qPages = Number(await card(page, "quotes.toscrape.com").locator('[data-field="pages"]').textContent());
  const qText = await card(page, "quotes.toscrape.com").textContent();
  check("multi-page site crawled", qStatus === "COMPLETED" && qPages >= 2, `${qStatus}, ${qPages} pages`);
  check("page limit respected (<= 8)", qPages <= 8, `${qPages}`);
  console.log("      summary:", qText.replace(/\s+/g, " ").slice(0, 260));
  await shot("k7-multipage");

  // --- SSRF through real DNS: a public-looking name that resolves to 127.0.0.1
  await page.fill("#url", "http://127.0.0.1.nip.io");
  await page.click("button:has-text('+ Add Website')");
  await card(page, "nip.io").waitFor({ timeout: 15000 });
  await card(page, "nip.io").locator('[data-status="FAILED"]').waitFor({ timeout: 60000 });
  const nipText = await card(page, "nip.io").textContent();
  check("DNS-to-loopback hostname is blocked by the worker", /cannot be crawled|could not find this website/i.test(nipText), nipText.replace(/\s+/g, " ").slice(0, 160));
  check("blocked crawl reveals no internal details", !/127\.0\.0\.1\b.*resolve|10\.|192\.168|Traceback|Exception/i.test(nipText.replace("127.0.0.1.nip.io", "")));
  await shot("k8-blocked");

  // --- delete (two-step confirm)
  await card(page, "quotes.toscrape.com").locator("button:has-text('Delete')").click();
  check("delete asks for confirmation first", (await card(page, "quotes.toscrape.com").textContent()).includes("Delete this source"));
  await card(page, "quotes.toscrape.com").locator("button:has-text('Yes, delete')").click();
  await page.waitForFunction(() => !document.body.innerText.includes("quotes.toscrape.com"), null, { timeout: 20000 });
  check("deleted source disappears", (await page.locator("li[data-source-id]:has-text('quotes.toscrape.com')").count()) === 0);

  // --- tenant isolation in the browser
  const sourceUrl = await (async () => {
    await Promise.all([page.waitForURL(/\/knowledge\/[0-9a-f-]{36}$/), card(page, "example.com").locator("a:has-text('Open')").click()]);
    return page.url();
  })();
  const other = await signupWithOrg(browser, "b", "Other Brand");
  await other.page.goto(`${BASE}/knowledge`);
  await other.page.waitForSelector("h1:has-text('Knowledge Base')", { timeout: 15000 });
  check("another organization sees an empty knowledge base", (await other.page.textContent("body")).includes("No sources yet") && !(await other.page.textContent("body")).includes("example.com"));
  const resp = await other.page.goto(sourceUrl);
  await other.page.waitForTimeout(1500);
  const otherBody = await other.page.textContent("body");
  check("another organization cannot open this source (404 page)", resp.status() === 404 || /could not be found|404/i.test(otherBody), `status ${resp.status()}`);
  check("...and none of its content leaks", !/Example Domain|documentation examples/.test(otherBody));
  await other.ctx.close();

  // --- logged-out access
  const anon = await browser.newContext();
  const ap = await anon.newPage();
  await ap.goto(`${BASE}/knowledge`);
  await ap.waitForURL(/\/login$/, { timeout: 15000 });
  check("logged-out /knowledge redirects to /login", at(ap, "/login"));
  await anon.close();

  check("no uncaught browser errors", errors.length === 0, errors.join(" | "));
  await ctx.close();
  await browser.close();
  console.log(failed === 0 ? "\nALL KNOWLEDGE E2E CHECKS PASSED" : `\n${failed} CHECK(S) FAILED`);
  process.exit(failed === 0 ? 0 : 1);
})().catch((e) => {
  console.error("E2E crashed:", e);
  process.exit(2);
});
