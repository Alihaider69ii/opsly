import assert from "node:assert/strict";
import { describe, test } from "node:test";
import {
  GENERIC_NOTICE,
  SHOPIFY_NOTICES,
  callbackRedirectPath,
  declinedInShopify,
  isActiveSync,
  noticeFor,
  safeAuthorizationUrl,
  webhookLabel,
} from "./shopify.ts";

const prod = { allowLocal: false };
const dev = { allowLocal: true };

describe("safeAuthorizationUrl", () => {
  test("accepts the real Shopify authorize URL", () => {
    const url = "https://my-store.myshopify.com/admin/oauth/authorize?client_id=x&state=abc";
    assert.equal(safeAuthorizationUrl(url, prod), url);
  });

  test("refuses anything that is not a myshopify.com store", () => {
    for (const bad of [
      "https://evil.example/admin/oauth/authorize",
      "https://myshopify.com/x",
      "https://.myshopify.com/x",
      "https://my-store.myshopify.com.evil.example/x",
      "https://evilmyshopify.com/x",
      "https://my-store.myshopify.com@evil.example/x",
      "https://user:pw@my-store.myshopify.com/x",
      "http://my-store.myshopify.com/x",
      "javascript:alert(1)",
      "data:text/html,<script>1</script>",
      "//evil.example/x",
      "/relative/path",
      "",
      "not a url",
    ]) {
      assert.equal(safeAuthorizationUrl(bad, prod), null, bad);
    }
    for (const notText of [undefined, null, 42, {}, ["https://a.myshopify.com"]]) {
      assert.equal(safeAuthorizationUrl(notText, prod), null);
    }
  });

  test("plain-http localhost is only allowed when developing", () => {
    const local = "http://localhost:9400/admin/oauth/authorize?state=abc";
    assert.equal(safeAuthorizationUrl(local, dev), local);
    assert.equal(safeAuthorizationUrl(local, prod), null);
    assert.equal(safeAuthorizationUrl("http://localhost.evil.example/x", dev), null);
    assert.equal(safeAuthorizationUrl("http://192.168.1.5/x", dev), null);
    assert.equal(safeAuthorizationUrl("ftp://localhost/x", dev), null);
  });
});

describe("notices", () => {
  test("known codes map to fixed copy", () => {
    for (const code of Object.keys(SHOPIFY_NOTICES))
      assert.equal(noticeFor(code), SHOPIFY_NOTICES[code]);
  });

  test("unknown, missing or hostile codes can never put their own words on the page", () => {
    for (const code of [
      undefined,
      "",
      "failed",
      "<script>alert(1)</script>",
      "__proto__",
      "constructor",
      "toString",
      "Your account is hacked, call 555-0100",
    ]) {
      assert.equal(noticeFor(code), GENERIC_NOTICE, String(code));
    }
  });
});

describe("callback handling", () => {
  test("redirects to the page with a success flag", () => {
    assert.equal(callbackRedirectPath({ ok: true }), "/integrations/shopify?connected=1");
  });

  test("an error code is passed through only if it looks like a code", () => {
    assert.equal(
      callbackRedirectPath({ ok: false, code: "invalid_state" }),
      "/integrations/shopify?error=invalid_state",
    );
    assert.equal(callbackRedirectPath({ ok: false }), "/integrations/shopify?error=failed");
    for (const code of ["../../evil", "a&b=c", "x y", "<b>", "A".repeat(80), "UPPER", "a/b"]) {
      assert.equal(
        callbackRedirectPath({ ok: false, code }),
        "/integrations/shopify?error=failed",
        code,
      );
    }
  });

  test("a merchant who declines in Shopify is recognised, a real callback is not", () => {
    assert.equal(declinedInShopify("?error=access_denied&shop=a.myshopify.com"), true);
    assert.equal(declinedInShopify("?code=abc&state=s&shop=a.myshopify.com&hmac=h"), false);
    assert.equal(declinedInShopify("?error=access_denied&code=abc"), false);
    assert.equal(declinedInShopify(""), false);
  });
});

describe("status helpers", () => {
  test("only SYNCING keeps the page refreshing", () => {
    assert.equal(isActiveSync("SYNCING"), true);
    for (const status of ["CONNECTED", "ERROR", "DISCONNECTED", undefined]) {
      assert.equal(isActiveSync(status), false);
    }
  });

  test("webhook labels are fixed text", () => {
    assert.match(webhookLabel("REGISTERED"), /on/);
    assert.match(webhookLabel("NOT_CONFIGURED"), /off/);
    assert.equal(webhookLabel("__proto__"), "Live updates: unknown");
  });
});
