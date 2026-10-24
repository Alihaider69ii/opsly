import assert from "node:assert/strict";
import { afterEach, beforeEach, describe, it } from "node:test";
import { readFileSync } from "node:fs";
import { JSDOM } from "jsdom";
import { MAX_MESSAGE_CHARS, destroy, init } from "../src/widget.js";

const KEY = "pk_testkey1234567890";
const API = "https://api.test";
const ROOT = `${API}/api/public/chat/${KEY}`;

let dom;
let window;
let document;

beforeEach(() => {
  dom = new JSDOM("<!doctype html><html><body><p id='host-page'>host page</p></body></html>", {
    url: "https://shop.test/",
    pretendToBeVisual: true,
  });
  window = dom.window;
  document = window.document;
});

afterEach(() => {
  destroy();
  window.close();
});

const tick = () => new Promise((resolve) => setTimeout(resolve, 0));
async function until(check, label = "condition") {
  for (let i = 0; i < 100; i++) {
    if (check()) return;
    await tick();
  }
  assert.fail(`timed out waiting for ${label}`);
}

/** A fake backend. Routes are "METHOD /path"; a route may return {status, body} or a promise. */
function backend(routes) {
  const calls = [];
  const fetch = async (url, init = {}) => {
    const method = init.method || "GET";
    const path = String(url).replace(ROOT, "");
    calls.push({ url: String(url), method, path, init, body: init.body ? JSON.parse(init.body) : undefined });
    const route = routes[`${method} ${path}`] || routes[`${method} ${path.replace(/\/conversations\/[^/]+$/, "/conversations/:id")}`];
    if (!route) return { ok: false, status: 404, json: async () => ({ detail: "not found" }) };
    const result = await (typeof route === "function" ? route(calls.at(-1)) : route);
    if (result instanceof Error) throw result;
    return { ok: result.status >= 200 && result.status < 300, status: result.status, json: async () => result.body };
  };
  return { fetch, calls };
}

const ok = (body, status = 200) => ({ status, body });
const CONFIG = ok({ business_name: "Luna Soap Co", welcome_message: "Welcome to Luna!" });
const CREATED = ok({ conversation_id: "c-1", token: "tok-1" }, 201);
const reply = (text) => ok({ conversation_id: "c-1", message: { role: "assistant", content: text, created_at: "2026-01-01T00:00:00Z" } });

async function start(routes, options = {}) {
  const api = backend({ "GET /config": CONFIG, "POST /conversations": CREATED, ...routes });
  const widget = await init({ key: KEY, api: API, window, document, fetch: api.fetch, ...options });
  return { widget, api };
}

function ui(widget) {
  const root = widget.host.shadowRoot;
  const q = (selector) => root.querySelector(selector);
  return {
    root,
    launcher: q(".bf-launcher"),
    panel: q(".bf-panel"),
    log: q(".bf-log"),
    input: q(".bf-input"),
    send: q(".bf-send"),
    status: q(".bf-status"),
    form: q(".bf-form"),
    texts: () => [...root.querySelectorAll(".bf-msg:not(.bf-typing)")].map((n) => n.textContent),
    messages: (kind) => [...root.querySelectorAll(`.bf-${kind}`)],
  };
}

async function say(widget, text) {
  const view = ui(widget);
  if (view.panel.hidden) view.launcher.click(); // a visitor opens the chat before typing
  view.input.value = text;
  view.form.requestSubmit();
  await tick();
}

describe("rendering", () => {
  it("shows a launcher and a hidden panel with the business name", async () => {
    const { widget } = await start({});
    const view = ui(widget);
    assert.ok(view.launcher);
    assert.equal(view.panel.hidden, true);
    assert.equal(view.root.querySelector(".bf-title").textContent, "Luna Soap Co");
    assert.equal(view.launcher.getAttribute("aria-expanded"), "false");
  });

  it("opens, greets the visitor once, and closes (launcher, close button, Escape)", async () => {
    const { widget } = await start({});
    const view = ui(widget);
    view.launcher.click();
    assert.equal(view.panel.hidden, false);
    assert.equal(view.launcher.getAttribute("aria-expanded"), "true");
    assert.deepEqual(view.texts(), ["Welcome to Luna!"]);
    view.root.querySelector(".bf-close").click();
    assert.equal(view.panel.hidden, true);
    view.launcher.click();
    assert.deepEqual(view.texts(), ["Welcome to Luna!"]); // not greeted twice
    view.panel.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    assert.equal(view.panel.hidden, true);
  });

  it("is accessible: dialog, live log, labelled input", async () => {
    const { widget } = await start({});
    const view = ui(widget);
    assert.equal(view.panel.getAttribute("role"), "dialog");
    assert.equal(view.log.getAttribute("role"), "log");
    assert.equal(view.log.getAttribute("aria-live"), "polite");
    assert.ok(view.input.getAttribute("aria-label"));
    assert.equal(view.input.getAttribute("maxlength"), String(MAX_MESSAGE_CHARS));
  });

  it("lives in a shadow root and does not touch the host page", async () => {
    const { widget } = await start({});
    assert.equal(document.getElementById("host-page").textContent, "host page");
    assert.ok(widget.host.shadowRoot);
    assert.equal(document.querySelectorAll(".bf-launcher").length, 0); // not in the light DOM
  });

  it("renders only once per page", async () => {
    const { widget } = await start({});
    const { widget: second } = await start({});
    assert.ok(widget);
    assert.equal(second, null);
    assert.equal(document.querySelectorAll("#bizflow-chat-host").length, 1);
  });
});

describe("when chat is unavailable nothing is rendered", () => {
  it("unknown or disabled key (404)", async () => {
    const { widget } = await start({ "GET /config": { status: 404, body: { detail: "Chat is not available" } } });
    assert.equal(widget, null);
    assert.equal(document.querySelectorAll("#bizflow-chat-host").length, 0);
  });

  it("backend unreachable", async () => {
    const { widget } = await start({ "GET /config": new Error("network down") });
    assert.equal(widget, null);
  });

  it("invalid key or API address never makes a request", async () => {
    for (const options of [{ key: "not-a-key" }, { key: "pk_x" }, { api: "" }, { api: "javascript:alert(1)" }, { api: "ftp://x" }]) {
      const api = backend({});
      const widget = await init({ key: KEY, api: API, window, document, fetch: api.fetch, ...options });
      assert.equal(widget, null);
      assert.equal(api.calls.length, 0);
    }
  });
});

describe("sending messages", () => {
  it("captures contact details only after the visitor submits the optional form", async () => {
    const { widget, api } = await start({ "POST /leads": ok({ status: "captured" }, 202) });
    assert.equal(api.calls.length, 1); // config lookup does not mint a customer or lead
    const view = ui(widget);
    view.root.querySelector(".bf-lead-toggle").click();
    const form = view.root.querySelector(".bf-lead-form");
    form.querySelector('[name="email"]').value = "visitor@example.com";
    form.querySelector('[name="marketing_consent"]').checked = true;
    form.requestSubmit();
    await until(() => api.calls.some((call) => call.path === "/leads"));
    const capture = api.calls.find((call) => call.path === "/leads");
    assert.equal(capture.body.email, "visitor@example.com");
    assert.equal(capture.body.marketing_consent, true);
    assert.equal(capture.init.headers["X-Conversation-Token"], "tok-1");
    assert.equal(capture.init.credentials, "omit");
  });

  it("creates a conversation, sends the message and shows the reply", async () => {
    const { widget, api } = await start({ "POST /messages": reply("We ship in two days.") });
    const view = ui(widget);
    view.launcher.click();
    await say(widget, "  How long is shipping?  ");
    await until(() => view.messages("assistant").length === 2, "assistant reply");
    assert.deepEqual(view.texts(), ["Welcome to Luna!", "How long is shipping?", "We ship in two days."]);
    assert.equal(view.input.value, "");

    const [config, created, sent] = api.calls;
    assert.equal(config.path, "/config");
    assert.equal(created.method, "POST");
    assert.equal(created.path, "/conversations");
    assert.match(created.body.visitor_id, /^[A-Za-z0-9_-]{8,64}$/);
    assert.equal(sent.path, "/messages");
    assert.deepEqual(sent.body, { conversation_id: "c-1", content: "How long is shipping?" });
    assert.equal(sent.init.headers["X-Conversation-Token"], "tok-1");
  });

  it("reuses the conversation for later messages", async () => {
    const { widget, api } = await start({ "POST /messages": reply("ok") });
    const view = ui(widget);
    await say(widget, "one");
    await until(() => view.messages("assistant").length === 2); // greeting + reply
    await say(widget, "two");
    await until(() => view.messages("assistant").length === 3);
    assert.equal(api.calls.filter((c) => c.path === "/conversations" && c.method === "POST").length, 1);
  });

  it("never sends cookies or credentials, and sends nothing but public data", async () => {
    const { widget, api } = await start({ "POST /messages": reply("ok") });
    await say(widget, "hello");
    await until(() => api.calls.some((c) => c.path === "/messages"));
    for (const call of api.calls) {
      assert.equal(call.init.credentials, "omit");
      assert.equal(call.init.mode, "cors");
      assert.ok(call.url.startsWith(`${ROOT}/`));
      const blob = JSON.stringify(call.init);
      assert.ok(!/organization|authorization|bearer|api[_-]?key|secret/i.test(blob), blob);
    }
  });

  it("ignores empty and whitespace-only messages", async () => {
    const { widget, api } = await start({ "POST /messages": reply("ok") });
    for (const text of ["", "   ", "\n\t "]) await say(widget, text);
    assert.equal(api.calls.filter((c) => c.path === "/messages").length, 0);
    assert.equal(ui(widget).messages("user").length, 0);
  });

  it("refuses over-long messages locally", async () => {
    const { widget, api } = await start({ "POST /messages": reply("ok") });
    await say(widget, "x".repeat(MAX_MESSAGE_CHARS + 1));
    assert.equal(api.calls.filter((c) => c.path === "/messages").length, 0);
    assert.match(ui(widget).status.textContent, /at most/);
  });

  it("Enter sends and Shift+Enter does not", async () => {
    const { widget, api } = await start({ "POST /messages": reply("ok") });
    const view = ui(widget);
    view.input.value = "line one";
    view.input.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Enter", shiftKey: true, bubbles: true, cancelable: true }));
    await tick();
    assert.equal(api.calls.filter((c) => c.path === "/messages").length, 0);
    view.input.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true }));
    await until(() => api.calls.some((c) => c.path === "/messages"));
  });
});

describe("loading state", () => {
  it("disables input while waiting, shows a typing indicator, then recovers", async () => {
    let release;
    const slow = new Promise((resolve) => {
      release = () => resolve(reply("Done waiting."));
    });
    const { widget } = await start({ "POST /messages": () => slow });
    const view = ui(widget);
    await say(widget, "hello");
    await until(() => view.input.disabled, "busy state");
    assert.equal(view.send.disabled, true);
    assert.equal(view.log.getAttribute("aria-busy"), "true");
    assert.ok(view.root.querySelector(".bf-typing"));
    assert.match(view.status.textContent, /typing/i);

    release();
    await until(() => !view.input.disabled, "idle state");
    assert.equal(view.send.disabled, false);
    assert.equal(view.root.querySelector(".bf-typing"), null);
    assert.equal(view.status.textContent, "");
    assert.ok(view.texts().includes("Done waiting."));
  });

  it("does not send a second message while one is in flight", async () => {
    let release;
    const slow = new Promise((resolve) => {
      release = () => resolve(reply("ok"));
    });
    const { widget, api } = await start({ "POST /messages": () => slow });
    await say(widget, "first");
    await until(() => ui(widget).input.disabled);
    await say(widget, "second");
    release();
    await until(() => !ui(widget).input.disabled);
    assert.equal(api.calls.filter((c) => c.path === "/messages").length, 1);
  });
});

describe("error state", () => {
  const cases = [
    [{ status: 503, body: { detail: "The assistant is temporarily unavailable." } }, /unavailable/i],
    [{ status: 429, body: { detail: "slow down" } }, /too quickly/i],
    [{ status: 409, body: { detail: "busy" } }, /wait for the previous reply/i],
    [{ status: 422, body: { detail: "too long" } }, /couldn't be sent/i],
    [{ status: 500, body: { detail: "boom" } }, /went wrong/i],
    [new Error("socket hang up"), /couldn't reach/i],
  ];
  for (const [response, pattern] of cases) {
    it(`shows a friendly message for ${response instanceof Error ? "a network failure" : response.status}`, async () => {
      const { widget } = await start({ "POST /messages": response });
      const view = ui(widget);
      await say(widget, "hello");
      await until(() => view.root.querySelector(".bf-error"), "error bubble");
      const text = view.root.querySelector(".bf-error").textContent;
      assert.match(text, pattern);
      assert.ok(!/boom|socket|hang up|detail|\{|Error:|undefined/.test(text), text); // no raw internals
      assert.equal(view.input.disabled, false); // the visitor can try again
      assert.equal(view.root.querySelector(".bf-error").getAttribute("role"), "alert");
    });
  }

  it("Try again resends the same message without duplicating it", async () => {
    let attempts = 0;
    const { widget, api } = await start({
      "POST /messages": () => (++attempts === 1 ? { status: 503, body: {} } : reply("Back online.")),
    });
    const view = ui(widget);
    await say(widget, "Is anyone there?");
    await until(() => view.root.querySelector(".bf-retry"));
    view.root.querySelector(".bf-retry").click();
    await until(() => view.texts().includes("Back online."));
    assert.equal(view.root.querySelector(".bf-error"), null);
    assert.equal(view.messages("user").length, 1); // not echoed twice
    assert.deepEqual(api.calls.filter((c) => c.path === "/messages").map((c) => c.body.content), ["Is anyone there?", "Is anyone there?"]);
  });

  it("an expired conversation is dropped and the next send starts a new one", async () => {
    let sends = 0;
    const { widget, api } = await start({
      "POST /conversations": () => ok({ conversation_id: `c-${++sends}`, token: `tok-${sends}` }, 201),
      "POST /messages": (call) => (call.body.conversation_id === "c-1" ? { status: 404, body: {} } : reply("Fresh start.")),
    });
    const view = ui(widget);
    await say(widget, "hello");
    await until(() => view.root.querySelector(".bf-retry"));
    assert.match(view.root.querySelector(".bf-error").textContent, /expired/i);
    view.root.querySelector(".bf-retry").click();
    await until(() => view.texts().includes("Fresh start."));
    assert.equal(api.calls.filter((c) => c.path === "/conversations" && c.method === "POST").length, 2);
  });
});

describe("safe rendering of untrusted text", () => {
  const attacks = [
    '<img src=x onerror="window.__pwned=1">',
    "<script>window.__pwned=1</script>",
    '<a href="javascript:window.__pwned=1">click</a>',
    '"><svg onload=window.__pwned=1>',
    "<style>body{display:none}</style>",
    "&lt;b&gt;encoded&lt;/b&gt;",
  ];
  for (const attack of attacks) {
    it(`renders ${JSON.stringify(attack.slice(0, 28))} as plain text, for both sides`, async () => {
      const { widget } = await start({ "POST /messages": reply(attack) });
      const view = ui(widget);
      await say(widget, attack);
      await until(() => view.messages("assistant").length === 2);
      assert.equal(view.messages("user")[0].textContent, attack); // shown literally
      assert.equal(view.messages("assistant")[1].textContent, attack);
      for (const tag of ["img", "script", "svg", "a", "style", "b"]) {
        assert.equal(view.log.querySelectorAll(tag).length, 0, `<${tag}> must not be created`);
      }
      assert.equal(window.__pwned, undefined);
      assert.equal(document.body.querySelectorAll("img, script, svg").length, 0);
    });
  }

  it("keeps line breaks without interpreting markup", async () => {
    const { widget } = await start({ "POST /messages": reply("line one\nline two") });
    const view = ui(widget);
    await say(widget, "hi");
    await until(() => view.messages("assistant").length === 2);
    assert.equal(view.messages("assistant")[1].textContent, "line one\nline two");
    assert.equal(view.log.querySelectorAll("br").length, 0);
  });

  it("the source never builds HTML from message text", () => {
    const source = readFileSync(new URL("../src/widget.js", import.meta.url), "utf8");
    assert.ok(!/innerHTML|outerHTML|insertAdjacentHTML|document\.write|eval\(|new Function/.test(source));
  });
});

describe("session and storage", () => {
  it("restores the conversation after a page reload (same tab session)", async () => {
    const sessionStorage = window.sessionStorage;
    const first = await start({ "POST /messages": reply("First answer.") });
    await say(first.widget, "First question");
    await until(() => ui(first.widget).messages("assistant").length === 2);
    destroy();

    const history = ok({
      conversation_id: "c-1",
      status: "OPEN",
      messages: [
        { role: "user", content: "First question", created_at: "x" },
        { role: "assistant", content: "First answer.", created_at: "x" },
      ],
    });
    const second = await start({ "GET /conversations/:id": history });
    assert.deepEqual(ui(second.widget).texts(), ["Welcome to Luna!", "First question", "First answer."]);
    const restored = second.api.calls.find((c) => c.path.startsWith("/conversations/"));
    assert.equal(restored.init.headers["X-Conversation-Token"], "tok-1");
    assert.ok(sessionStorage.getItem(`bizflow_chat_${KEY}`));
  });

  it("forgets a saved conversation the server no longer recognizes", async () => {
    window.sessionStorage.setItem(`bizflow_chat_${KEY}`, JSON.stringify({ id: "gone", token: "stale" }));
    const { widget } = await start({ "GET /conversations/:id": { status: 404, body: {} } });
    assert.deepEqual(ui(widget).texts(), []);
    assert.equal(window.sessionStorage.getItem(`bizflow_chat_${KEY}`), null);
  });

  it("ignores corrupted saved state", async () => {
    window.sessionStorage.setItem(`bizflow_chat_${KEY}`, "{not json");
    const { widget } = await start({});
    assert.ok(widget);
    assert.equal(window.sessionStorage.getItem(`bizflow_chat_${KEY}`), null);
  });

  it("keeps one anonymous visitor id", async () => {
    const { widget, api } = await start({ "POST /messages": reply("ok") });
    await say(widget, "hi");
    await until(() => api.calls.some((c) => c.path === "/messages"));
    const stored = window.localStorage.getItem("bizflow_visitor");
    assert.match(stored, /^[A-Za-z0-9_-]{8,64}$/);
    assert.equal(api.calls.find((c) => c.path === "/conversations" && c.method === "POST").body.visitor_id, stored);
  });

  it("works when storage is blocked", async () => {
    const blocked = {
      getItem() { throw new Error("denied"); },
      setItem() { throw new Error("denied"); },
      removeItem() { throw new Error("denied"); },
    };
    const { widget } = await start({ "POST /messages": reply("Still works.") }, { localStorage: blocked, sessionStorage: blocked });
    const view = ui(widget);
    await say(widget, "hi");
    await until(() => view.texts().includes("Still works."));
  });

  it("destroy removes the widget from the page", async () => {
    const { widget } = await start({});
    widget.destroy();
    assert.equal(document.querySelectorAll("#bizflow-chat-host").length, 0);
  });
});

describe("starting and stopping while the network is slow", () => {
  /** A promise the test opens by hand, to hold a request "in flight". */
  function hold() {
    let open;
    const closed = new Promise((resolve) => (open = resolve));
    return { closed, open };
  }
  const hosts = () => document.querySelectorAll("#bizflow-chat-host").length;
  const options = (api) => ({ key: KEY, api: API, window, document, fetch: api.fetch });

  it("two starts at once mount exactly one widget", async () => {
    const config = hold();
    const api = backend({
      "GET /config": async () => (await config.closed, CONFIG),
      "POST /conversations": CREATED,
    });
    const first = init(options(api));
    const second = init(options(api));
    config.open();
    const [a, b] = await Promise.all([first, second]);
    assert.equal(hosts(), 1);
    assert.ok(a && b === null, "exactly one of the two starts owns the widget");
    assert.equal(api.calls.filter((c) => c.path === "/config").length, 1); // and one config request
  });

  it("leaving the page while the config is loading leaves no widget behind", async () => {
    const config = hold();
    const api = backend({ "GET /config": async () => (await config.closed, CONFIG) });
    const pending = init(options(api));
    destroy(); // the page was left before the answer arrived
    config.open();
    assert.equal(await pending, null);
    assert.equal(hosts(), 0, "a launcher must not appear on a page that has been left");
  });

  it("start, stop, start (what React does in development) ends with exactly one widget", async () => {
    const config = hold();
    const api = backend({
      "GET /config": async () => (await config.closed, CONFIG),
      "POST /conversations": CREATED,
    });
    const first = init(options(api));
    destroy();
    const second = init(options(api));
    config.open();
    const [a, b] = await Promise.all([first, second]);
    assert.equal(a, null, "the cancelled start must not mount");
    assert.ok(b, "the current start must");
    assert.equal(hosts(), 1);
    destroy();
    assert.equal(hosts(), 0, "and destroy removes the one that exists");
  });

  it("leaving the page while a saved conversation is loading leaves no widget behind", async () => {
    window.sessionStorage.setItem(`bizflow_chat_${KEY}`, JSON.stringify({ id: "c-1", token: "tok-1" }));
    const history = hold();
    const api = backend({
      "GET /config": CONFIG,
      "GET /conversations/:id": async () => (await history.closed, ok({ conversation_id: "c-1", status: "OPEN", messages: [] })),
    });
    const pending = init(options(api));
    await until(() => api.calls.some((c) => c.path.startsWith("/conversations/")), "the history request");
    destroy();
    history.open();
    assert.equal(await pending, null);
    assert.equal(hosts(), 0);
  });

  it("two copies of the bundle on one page still mount exactly one widget", async () => {
    // Each loaded copy of widget.js has its own module scope: a snippet pasted twice, or a page
    // effect that appends the script twice, must not give every copy its own "one widget".
    const other = await import(`../src/widget.js?second-copy=${Date.now()}`);
    assert.notEqual(other.init, init, "this must be a genuinely separate module instance");
    const config = hold();
    const api = backend({
      "GET /config": async () => (await config.closed, CONFIG),
      "POST /conversations": CREATED,
    });
    const first = init(options(api));
    const second = other.init(options(api));
    config.open();
    const [a, b] = await Promise.all([first, second]);
    assert.equal(hosts(), 1);
    assert.ok(a && b === null);
    other.destroy(); // either copy can remove the widget
    assert.equal(hosts(), 0);
  });

  it("leaving the page through another copy of the bundle cancels a start that is waiting", async () => {
    const other = await import(`../src/widget.js?third-copy=${Date.now()}`);
    const config = hold();
    const api = backend({ "GET /config": async () => (await config.closed, CONFIG) });
    const pending = init(options(api));
    other.destroy();
    config.open();
    assert.equal(await pending, null);
    assert.equal(hosts(), 0);
  });

  it("a new start is allowed once the previous widget was destroyed", async () => {
    const api = backend({ "GET /config": CONFIG });
    const first = await init(options(api));
    assert.ok(first);
    destroy();
    assert.ok(await init(options(api)));
    assert.equal(hosts(), 1);
  });

  it("a failed start does not block the next one", async () => {
    const down = backend({ "GET /config": { status: 503, body: {} } });
    assert.equal(await init(options(down)), null);
    const up = backend({ "GET /config": CONFIG });
    assert.ok(await init(options(up)));
    assert.equal(hosts(), 1);
  });
});

describe("the bundle is public-safe", () => {
  it("contains no secrets, keys or privileged endpoints", () => {
    const sources = ["../src/widget.js", "../src/styles.js"].map((p) => readFileSync(new URL(p, import.meta.url), "utf8")).join("\n");
    assert.ok(!/sk-[A-Za-z0-9]{8,}|OPENAI|Bearer |x-api-key|password|\/api\/(auth|organizations|knowledge|chat\/widget)/i.test(sources));
    assert.ok(/\/api\/public\/chat\//.test(sources)); // the only API surface it knows
  });
});
