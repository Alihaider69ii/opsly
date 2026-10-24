const cfg = require("./lib/config");
const path = require("path");

const BASE = cfg.BASE;
const SHOTS = cfg.ARTIFACTS;
const stamp = Date.now();
const email = `E2E-${stamp}@Example.com`; // mixed case on purpose: must be normalized
const normalized = email.toLowerCase();
const password = "correct-horse-battery";
const bizName = "L'avenir Skins";

let failed = 0;
function check(name, ok, extra = "") {
  if (!ok) failed++;
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${extra ? `   [${extra}]` : ""}`);
}
const endsWith = (page, p) => new URL(page.url()).pathname === p;
const settleAt = async (page, p) => {
  try { await page.waitForURL((u) => new URL(u).pathname === p, { timeout: 15000 }); } catch {}
};
const ALERT = "form [role=alert]"; // scoped: Next.js also renders an invisible route announcer with role=alert

(async () => {
  const browser = await cfg.launchBrowser();
  const ctx = await browser.newContext({ viewport: { width: 1000, height: 800 } });
  const page = await ctx.newPage();
  const shot = (n) => page.screenshot({ caret: "initial", path: path.join(SHOTS, `${n}.png`) });
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));

  // --- route guards when logged out
  await page.goto(`${BASE}/dashboard`);
  await settleAt(page, "/login");
  check("logged out: /dashboard -> /login", endsWith(page, "/login"), page.url());
  await page.goto(`${BASE}/onboarding`);
  check("logged out: /onboarding -> /login", endsWith(page, "/login"), page.url());
  await shot("01-login");

  // --- signup: server-side validation errors are shown (bypass browser validation)
  await page.goto(`${BASE}/signup`);
  await page.fill("#email", email);
  await page.fill("#password", "short");
  await page.evaluate(() => (document.querySelector("form").noValidate = true));
  await page.click("button[type=submit]");
  await page.waitForSelector("#password-error", { timeout: 15000 });
  const pwErr = await page.textContent("#password-error");
  check("signup: weak password shows backend field error", /at least 10/i.test(pwErr), pwErr);
  check("signup: email is kept after error", (await page.inputValue("#email")) === email);
  check("signup: password is NOT repopulated", (await page.inputValue("#password")) === "");
  await shot("02-signup-error");

  // --- signup success -> onboarding
  await page.fill("#full_name", "E2E Tester");
  await page.fill("#password", password);
  await Promise.all([page.waitForURL(/\/onboarding$/, { timeout: 20000 }), page.click("button[type=submit]")]);
  check("signup success lands on /onboarding", endsWith(page, "/onboarding"));
  await shot("03-onboarding");

  // --- session storage security
  const cookie = (await ctx.cookies()).find((c) => c.name === "bizflow_session");
  check("session cookie exists", !!cookie);
  check("cookie is HttpOnly", cookie?.httpOnly === true);
  check("cookie SameSite=Lax", cookie?.sameSite === "Lax");
  check("cookie path=/", cookie?.path === "/");
  check("JS cannot read cookie (document.cookie)", !(await page.evaluate(() => document.cookie)).includes("bizflow_session"));
  check("nothing stored in local/sessionStorage", (await page.evaluate(() => localStorage.length + sessionStorage.length)) === 0);
  const html = await page.content();
  check("no JWT in page HTML / RSC payload", !/eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}/.test(html));
  check("backend URL not exposed in page HTML", !html.includes(new URL(cfg.API).host));

  // --- onboarding: only the first business is created via this flow
  await page.fill("#name", bizName);
  await page.selectOption("#business_type", "beauty_skincare");
  await page.fill("#website", "example.com"); // scheme-less on purpose
  await Promise.all([page.waitForURL(/\/dashboard$/, { timeout: 20000 }), page.click("button[type=submit]")]);
  check("onboarding success lands on /dashboard", endsWith(page, "/dashboard"));
  await page.waitForSelector("h1:has-text('Welcome back')", { timeout: 15000 });

  const body = await page.textContent("body");
  check("dashboard: welcome shows normalized email", body.includes(`Welcome back, ${normalized}`), normalized);
  check("dashboard: business name", body.includes(bizName));
  check("dashboard: website normalized to https", body.includes("https://example.com/"));
  check("dashboard: business type label", body.includes("Beauty & Skincare"));
  check("dashboard: creator is OWNER", /Your role\s*OWNER/.test(body));
  await shot("04-dashboard");

  await page.goto(`${BASE}/onboarding`);
  await settleAt(page, "/dashboard");
  check("with a business, /onboarding -> /dashboard", endsWith(page, "/dashboard"), page.url());

  // --- logout
  await Promise.all([page.waitForURL(/\/login$/, { timeout: 15000 }), page.click("text=Log out")]);
  check("logout lands on /login", endsWith(page, "/login"));
  check("logout clears the session cookie", !(await ctx.cookies()).some((c) => c.name === "bizflow_session"));
  await page.goto(`${BASE}/dashboard`);
  await settleAt(page, "/login");
  check("after logout: /dashboard -> /login", endsWith(page, "/login"));

  // --- login: wrong password, then right password
  await page.fill("#email", normalized);
  await page.fill("#password", "definitely-the-wrong-one");
  await page.click("button[type=submit]");
  await page.waitForSelector(ALERT, { timeout: 15000 });
  const alert = await page.textContent(ALERT);
  check("login: wrong password shows generic error", /Invalid email or password/.test(alert), alert);
  check("login: still on /login", endsWith(page, "/login"));
  await shot("05-login-error");

  await page.fill("#password", password);
  await Promise.all([page.waitForURL(/\/dashboard$/, { timeout: 20000 }), page.click("button[type=submit]")]);
  check("login success (existing business) lands on /dashboard", endsWith(page, "/dashboard"));
  await page.waitForSelector("h1:has-text('Welcome back')", { timeout: 15000 });
  await page.goto(`${BASE}/login`);
  await settleAt(page, "/dashboard");
  check("signed-in user visiting /login is sent to /dashboard", endsWith(page, "/dashboard"), page.url());

  // --- duplicate signup
  const ctx2 = await browser.newContext();
  const p2 = await ctx2.newPage();
  await p2.goto(`${BASE}/signup`);
  await p2.fill("#email", normalized);
  await p2.fill("#password", password);
  await p2.click("button[type=submit]");
  await p2.waitForSelector(ALERT, { timeout: 15000 });
  check("duplicate signup shows error", /already exists/i.test(await p2.textContent(ALERT)));
  await ctx2.close();

  // --- stale / garbage cookie must not cause a redirect loop
  const ctx3 = await browser.newContext();
  await ctx3.addCookies([{ name: "bizflow_session", value: "garbage.token.value", url: BASE, httpOnly: true }]);
  const p3 = await ctx3.newPage();
  await p3.goto(`${BASE}/dashboard`, { waitUntil: "load" });
  await p3.waitForURL(/\/login$/, { timeout: 15000 });
  check("garbage cookie: /dashboard -> /login (no loop)", endsWith(p3, "/login"), p3.url());
  check("garbage cookie: login form still renders", (await p3.locator("#email").count()) === 1);
  await ctx3.close();

  // --- a second tenant sees only their own business
  const ctx4 = await browser.newContext();
  const p4 = await ctx4.newPage();
  const email2 = `e2e-other-${stamp}@example.com`;
  await p4.goto(`${BASE}/signup`);
  await p4.fill("#email", email2);
  await p4.fill("#password", password);
  await Promise.all([p4.waitForURL(/\/onboarding$/, { timeout: 20000 }), p4.click("button[type=submit]")]);
  await p4.fill("#name", "Other Brand Co");
  await Promise.all([p4.waitForURL(/\/dashboard$/, { timeout: 20000 }), p4.click("button[type=submit]")]);
  await p4.waitForSelector("h1:has-text('Welcome back')", { timeout: 15000 });
  const body4 = await p4.textContent("body");
  check("tenant B dashboard shows B's business", body4.includes("Other Brand Co") && body4.includes(email2));
  check("tenant B sees none of tenant A's data", !body4.includes(bizName) && !body4.includes(normalized));
  await ctx4.close();

  check("no uncaught browser errors", errors.length === 0, errors.join(" | "));
  await browser.close();
  console.log(failed === 0 ? "\nALL E2E CHECKS PASSED" : `\n${failed} E2E CHECK(S) FAILED`);
  process.exit(failed === 0 ? 0 : 1);
})().catch((e) => {
  console.error("E2E crashed:", e);
  process.exit(2);
});
