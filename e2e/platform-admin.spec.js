// Browser coverage for the Platform Admin layer: platform login, the dashboard, brand creation,
// view-as-brand (enter/exit/switch) with the required banner, cross-brand data isolation, and
// that a plain brand user can never reach /admin. The e2e database starts empty each run and has
// no public "become a platform admin" path (by design), so this suite registers its platform
// admin the same way the Shopify suite grabs a bearer token directly (see shopify.spec.js
// apiToken()) and then flips is_platform_admin via cfg.sql -- exactly the "direct DB write for
// test setup the API deliberately does not expose" pattern already used by registerFakeInbound()
// in support.spec.js.
const cfg = require("./lib/config");
const path = require("path");

const { BASE, API } = cfg;
const SHOTS = cfg.ARTIFACTS;
const stamp = Date.now();
const adminEmail = `platform-admin-${stamp}@example.com`;
const adminPassword = "correct-horse-battery";

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

async function makePlatformAdmin() {
  const r = await fetch(`${API}/api/auth/register`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ email: adminEmail, password: adminPassword }),
  });
  if (!r.ok) throw new Error(`register failed: ${r.status} ${await r.text()}`);
  cfg.sql(`UPDATE users SET is_platform_admin = true WHERE email = '${adminEmail}'`);
}

(async () => {
  await makePlatformAdmin();

  const browser = await cfg.launchBrowser();
  const ctx = await browser.newContext({ viewport: { width: 1200, height: 900 } });
  const page = await ctx.newPage();
  const shot = (n) => page.screenshot({ caret: "initial", path: path.join(SHOTS, `platform-admin-${n}.png`) });
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));

  // --- 1/17: unauthorized user cannot access /admin
  await page.goto(`${BASE}/admin`);
  await settleAt(page, "/admin/login");
  check("logged out: /admin -> /admin/login", endsWith(page, "/admin/login"), page.url());

  // --- 2: platform admin login
  await page.fill("#email", adminEmail);
  await page.fill("#password", adminPassword);
  await Promise.all([page.waitForURL(/\/admin$/, { timeout: 20000 }), page.click("button[type=submit]")]);
  check("platform admin login lands on /admin", endsWith(page, "/admin"));

  // --- 3: platform dashboard loads with real numbers (Suspense-streamed: wait past the fallback)
  await page.waitForSelector("text=Total brands", { timeout: 15000 });
  check("platform dashboard shows Total brands", true);
  await shot("01-dashboard");

  // --- 4/5/6: brand list + create a demo brand via the UI
  await page.goto(`${BASE}/admin/brands`);
  const brandAName = `E2E Brand A ${stamp}`;
  const brandASlug = `e2e-brand-a-${stamp}`;
  await page.fill("#name", brandAName);
  await page.fill("#slug", brandASlug);
  await page.selectOption("#environment", "DEMO");
  await page.fill("#owner_name", "Brand A Owner");
  const ownerAEmail = `owner-a-${stamp}@example.com`;
  await page.fill("#owner_email", ownerAEmail);
  await page.fill("#temporary_password", "correct-horse-battery");
  await page.click("button:has-text('Create Brand')");
  await page.waitForSelector(`li[data-brand="${brandASlug}"]`, { timeout: 15000 });
  check("brand A appears in the list after creation", (await page.locator(`li[data-brand="${brandASlug}"]`).count()) === 1);
  await shot("02-brand-created");

  // --- a second brand, for the cross-brand isolation check below
  const brandBName = `E2E Brand B ${stamp}`;
  const brandBSlug = `e2e-brand-b-${stamp}`;
  await page.fill("#name", brandBName);
  await page.fill("#slug", brandBSlug);
  await page.selectOption("#environment", "DEMO");
  await page.fill("#owner_name", "Brand B Owner");
  await page.fill("#owner_email", `owner-b-${stamp}@example.com`);
  await page.fill("#temporary_password", "correct-horse-battery");
  await page.click("button:has-text('Create Brand')");
  await page.waitForSelector(`li[data-brand="${brandBSlug}"]`, { timeout: 15000 });
  check("brand B appears in the list after creation", (await page.locator(`li[data-brand="${brandBSlug}"]`).count()) === 1);

  // --- 7/8/9: open workspace, verify the banner + the brand's own dashboard, then exit
  await page.locator(`li[data-brand="${brandASlug}"] button:has-text("Open Workspace")`).click();
  await settleAt(page, "/dashboard");
  check("Open Workspace lands on the brand's /dashboard", endsWith(page, "/dashboard"));
  await page.waitForSelector("h1:has-text('Welcome back')", { timeout: 15000 });
  let body = await page.textContent("body");
  check("PLATFORM ADMIN VIEW banner is shown", body.includes("PLATFORM ADMIN VIEW"));
  check("banner names the viewed brand (A)", body.includes(brandAName));
  check("brand A's own dashboard shows its name", body.includes(brandAName));
  await shot("03-view-as-brand-a");

  await page.click("text=Exit Brand View");
  await settleAt(page, "/admin/brands");
  check("Exit Brand View returns to /admin/brands", endsWith(page, "/admin/brands"));
  body = await page.textContent("body");
  check("banner is gone after exiting", !body.includes("PLATFORM ADMIN VIEW"));

  // --- 10: switch to brand B -- brand A's data/name must not leak
  await page.locator(`li[data-brand="${brandBSlug}"] button:has-text("Open Workspace")`).click();
  await settleAt(page, "/dashboard");
  // "Welcome back" only ever renders on the brand's own /dashboard, never on /admin/brands --
  // brandBName's text is ambiguous (it also appears in the admin brand list itself), so waiting
  // on it alone could resolve against the list page's own row rather than the new page.
  await page.waitForSelector("h1:has-text('Welcome back')", { timeout: 15000 });
  const bannerB = await page.locator("text=PLATFORM ADMIN VIEW").locator("..").textContent();
  check(
    "banner now names brand B, not brand A",
    bannerB.includes(brandBName) && !bannerB.includes(brandAName),
    bannerB,
  );
  await shot("04-view-as-brand-b-no-leak");
  await page.click("text=Exit Brand View");
  await settleAt(page, "/admin/brands");

  // --- 11: deactivate / activate a demo brand
  await page.locator(`li[data-brand="${brandASlug}"] button:has-text("Deactivate")`).click();
  await page.waitForSelector(`li[data-brand="${brandASlug}"]:has-text("INACTIVE")`, { timeout: 15000 });
  check("brand A shows INACTIVE after deactivation", (await page.locator(`li[data-brand="${brandASlug}"]:has-text("INACTIVE")`).count()) === 1);
  await page.locator(`li[data-brand="${brandASlug}"] button:has-text("Activate")`).click();
  await page.waitForSelector(`li[data-brand="${brandASlug}"]:not(:has-text("INACTIVE"))`, { timeout: 15000 });
  check("brand A no longer shows INACTIVE after reactivation", (await page.locator(`li[data-brand="${brandASlug}"]:has-text("INACTIVE")`).count()) === 0);

  // --- platform audit shows real events attributed to the admin, never a synthetic actor
  await page.goto(`${BASE}/admin/audit`);
  const auditBody = await page.textContent("body");
  check("platform activity log shows BRAND_CREATED", auditBody.includes("BRAND_CREATED"));
  check("platform activity log shows PLATFORM_ADMIN_ENTERED_BRAND", auditBody.includes("PLATFORM_ADMIN_ENTERED_BRAND"));
  check("platform activity log attributes events to the admin's own email", auditBody.includes(adminEmail));

  // --- 16: logout
  await Promise.all([page.waitForURL(/\/admin\/login$/, { timeout: 15000 }), page.click("text=Log out")]);
  check("platform admin logout lands on /admin/login", endsWith(page, "/admin/login"));

  // --- 17: a plain brand user can never reach /admin
  const ctx2 = await browser.newContext();
  const p2 = await ctx2.newPage();
  const plainEmail = `plain-${stamp}@example.com`;
  await p2.goto(`${BASE}/signup`);
  await p2.fill("#email", plainEmail);
  await p2.fill("#password", adminPassword);
  await Promise.all([p2.waitForURL(/\/onboarding$/, { timeout: 20000 }), p2.click("button[type=submit]")]);
  await p2.fill("#name", "Plain Brand");
  await Promise.all([p2.waitForURL(/\/dashboard$/, { timeout: 20000 }), p2.click("button[type=submit]")]);
  await p2.goto(`${BASE}/admin`);
  await settleAt(p2, "/admin/login");
  check("a plain brand user visiting /admin is sent to /admin/login", endsWith(p2, "/admin/login"), p2.url());
  await ctx2.close();

  check("no uncaught browser errors", errors.length === 0, errors.join(" | "));
  await browser.close();
  console.log(failed === 0 ? "\nALL E2E CHECKS PASSED" : `\n${failed} E2E CHECK(S) FAILED`);
  process.exit(failed === 0 ? 0 : 1);
})().catch((e) => {
  console.error("E2E crashed:", e);
  process.exit(2);
});
