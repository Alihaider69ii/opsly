import assert from "node:assert/strict";
import { describe, test } from "node:test";
import config from "../../next.config.ts";

async function headersFor(env: string): Promise<Map<string, string>> {
  const previous = process.env.NODE_ENV;
  (process.env as Record<string, string>).NODE_ENV = env;
  try {
    const rules = await config.headers!();
    assert.equal(rules.length, 1);
    assert.equal(rules[0].source, "/:path*"); // every route, including the OAuth callback
    return new Map(rules[0].headers.map((h) => [h.key, h.value]));
  } finally {
    (process.env as Record<string, string>).NODE_ENV = previous ?? "test";
  }
}

describe("dashboard security headers", () => {
  test("every response forbids framing, sniffing and leaky referrers", async () => {
    const h = await headersFor("development");
    assert.equal(h.get("X-Frame-Options"), "DENY");
    assert.equal(h.get("X-Content-Type-Options"), "nosniff");
    assert.equal(h.get("Referrer-Policy"), "strict-origin-when-cross-origin");
    assert.match(h.get("Permissions-Policy") ?? "", /camera=\(\)/);
  });

  test("the policy blocks framing, base-tag injection and plugins", async () => {
    const csp = (await headersFor("development")).get("Content-Security-Policy") ?? "";
    assert.match(csp, /frame-ancestors 'none'/);
    assert.match(csp, /base-uri 'self'/);
    assert.match(csp, /object-src 'none'/);
  });

  test("it does not set directives that would break the app", async () => {
    const csp = (await headersFor("production")).get("Content-Security-Policy") ?? "";
    assert.doesNotMatch(csp, /script-src/); // Next injects inline scripts that need nonces
    assert.doesNotMatch(csp, /form-action/); // would block the redirect to Shopify's authorize page
  });

  test("HSTS is sent in production only", async () => {
    assert.match(
      (await headersFor("production")).get("Strict-Transport-Security") ?? "",
      /max-age=63072000; includeSubDomains/,
    );
    assert.equal((await headersFor("development")).has("Strict-Transport-Security"), false);
  });

  test("the framework is not advertised", () => {
    assert.equal(config.poweredByHeader, false);
  });
});
