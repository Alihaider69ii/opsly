// Every dashboard page fits a phone: no horizontal scrolling and nothing wider than the viewport.
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
    const ctx = await browser.newContext({ viewport: { width: 390, height: 844 } });
    const page = await ctx.newPage();
    await page.goto(`${BASE}/signup`);
    await page.fill("#email", `ovf-${stamp}@example.com`);
    await page.fill("#password", "correct-horse-battery");
    await Promise.all([page.waitForURL(/\/onboarding$/), page.click("button[type=submit]")]);
    await page.fill("#name", "Overflow Co");
    await Promise.all([page.waitForURL(/\/dashboard$/), page.click("button[type=submit]")]);

    for (const route of ["/dashboard", "/knowledge", "/website-chat", "/integrations/shopify"]) {
      await page.goto(BASE + route);
      await page.waitForSelector("main");
      const result = await page.evaluate(() => {
        const width = window.innerWidth;
        const wide = [];
        for (const el of document.querySelectorAll("body *")) {
          const r = el.getBoundingClientRect();
          if (r.right > width + 1 && r.width > 0) {
            wide.push(`${el.tagName.toLowerCase()}.${String(el.className).split(" ")[0]} right=${Math.round(r.right)}`);
          }
        }
        return { scrollWidth: document.documentElement.scrollWidth, inner: width, wide: wide.slice(0, 6) };
      });
      check(`${route}: no horizontal scroll on a 390px phone`, result.scrollWidth <= result.inner + 1, JSON.stringify(result));
      check(`${route}: no element wider than the screen`, result.wide.length === 0, result.wide.join(", "));
    }
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
