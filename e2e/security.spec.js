// What a real browser sees of the dashboard's security controls: response headers, the session
// cookie's flags, and that the session token never reaches page scripts.
const cfg = require("./lib/config");

const { BASE } = cfg;
const stamp = Date.now();
let failed = 0;
function check(name, ok, extra = "") {
  if (!ok) failed++;
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${extra ? `   [${String(extra).slice(0, 240)}]` : ""}`);
}

(async () => {
  const browser = await cfg.launchBrowser();
  try {
    const ctx = await browser.newContext({ viewport: { width: 1100, height: 800 } });
    const page = await ctx.newPage();

    // --- headers on a public page -----------------------------------------------------------------
    const login = await page.goto(`${BASE}/login`);
    const h = login.headers();
    check("X-Frame-Options: DENY", h["x-frame-options"] === "DENY", h["x-frame-options"]);
    check("X-Content-Type-Options: nosniff", h["x-content-type-options"] === "nosniff");
    check("Referrer-Policy is set", /strict-origin-when-cross-origin/.test(h["referrer-policy"] || ""));
    check("CSP forbids framing", /frame-ancestors 'none'/.test(h["content-security-policy"] || ""));
    check("the framework is not advertised", !("x-powered-by" in h), h["x-powered-by"]);

    // --- the dashboard cannot be framed by another site -------------------------------------------------
    const framer = await ctx.newPage();
    await framer.setContent(`<iframe id="f" src="${BASE}/login" width="600" height="400"></iframe>`);
    await framer.waitForTimeout(1500);
    const frame = framer.frames().find((f) => f.url().startsWith(BASE));
    const framedBody = frame ? await frame.evaluate(() => document.body.innerText.length).catch(() => 0) : 0;
    check("the login page does not render inside another page's iframe", framedBody === 0, `chars=${framedBody}`);
    await framer.close();

    // --- sign up, then inspect the session cookie ------------------------------------------------------
    await page.goto(`${BASE}/signup`);
    await page.fill("#email", `sec-${stamp}@example.com`);
    await page.fill("#password", "correct-horse-battery");
    await Promise.all([page.waitForURL(/\/onboarding$/), page.click("button[type=submit]")]);

    const cookies = await ctx.cookies(BASE);
    const session = cookies.find((c) => c.name === "bizflow_session");
    check("a session cookie was set", Boolean(session));
    check("it is HttpOnly (no script can read it)", session?.httpOnly === true);
    check("it is SameSite=Lax", session?.sameSite === "Lax", session?.sameSite);
    const insecureOk = new URL(BASE).protocol === "http:"; // Secure is only set when served over https
    check("it is Secure when served over https", insecureOk || session?.secure === true, `secure=${session?.secure}`);
    check("it expires (not a forever cookie)", (session?.expires ?? -1) > 0);

    const visible = await page.evaluate(() => document.cookie);
    check("document.cookie does not expose the session", !visible.includes("bizflow_session"), visible);
    const html = await page.content();
    check("the session token is not in the page HTML", !session || !html.includes(session.value));
    const storage = await page.evaluate(() => JSON.stringify([localStorage, sessionStorage]));
    check("nothing is kept in localStorage/sessionStorage", storage === "[{},{}]", storage);

    // --- logging out removes access ---------------------------------------------------------------------
    await ctx.clearCookies();
    await page.goto(`${BASE}/dashboard`);
    check("without the cookie a protected page redirects to /login", new URL(page.url()).pathname === "/login");

    // --- a tampered cookie is not a session -----------------------------------------------------------------
    await ctx.addCookies([{ name: "bizflow_session", value: "not-a-real-token", url: BASE }]);
    await page.goto(`${BASE}/dashboard`);
    await page.waitForLoadState("networkidle");
    check("a forged session cookie does not open the dashboard", new URL(page.url()).pathname === "/login", page.url());
    await ctx.close();
  } catch (e) {
    failed++;
    console.log("FAIL  unexpected error:", e.stack || e);
  } finally {
    await browser.close();
  }
  console.log(failed ? `\n${failed} CHECK(S) FAILED` : "\nALL CHECKS PASSED");
  process.exit(failed ? 1 : 0);
})();
