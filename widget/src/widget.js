// BizFlow website chat widget.
//
// Embed:  <script src="https://YOUR-HOST/widget.js" data-key="pk_..." data-api="https://API-HOST" async></script>
//
// Security model: everything in this file is public. It holds only the business's PUBLIC key (an
// identifier, not a credential) and a per-conversation token minted by the server. There are no API
// secrets, no LLM keys and no privileged tokens. Message text, from the visitor or the assistant, is
// only ever inserted with textContent, never as HTML.

import { STYLES } from "./styles.js";

export const MAX_MESSAGE_CHARS = 2000;
const REQUEST_TIMEOUT_MS = 45000;
const KEY_RE = /^pk_[A-Za-z0-9_-]{8,64}$/;
const VISITOR_STORAGE_KEY = "bizflow_visitor";
const SESSION_STORAGE_PREFIX = "bizflow_chat_";

const ERRORS = {
  network: "We couldn't reach the chat service. Check your connection and try again.",
  unavailable: "Our assistant is unavailable right now. Please try again in a moment.",
  rate: "You're sending messages too quickly. Please wait a moment and try again.",
  busy: "Please wait for the previous reply before sending another message.",
  expired: "This chat session expired. Please send your message again.",
  limit: "This conversation has reached its message limit. Please refresh to start a new one.",
  rejected: "That message couldn't be sent. Please shorten it and try again.",
  generic: "Something went wrong. Please try again.",
};

function randomId() {
  const c = globalThis.crypto;
  if (c && typeof c.randomUUID === "function") return c.randomUUID();
  let id = "";
  for (let i = 0; i < 32; i++) id += Math.floor(Math.random() * 16).toString(16);
  return id;
}

// Storage can throw (private mode, blocked cookies); chat must keep working without it.
function safeStorage(storage) {
  return {
    get(key) {
      try {
        return storage ? storage.getItem(key) : null;
      } catch {
        return null;
      }
    },
    set(key, value) {
      try {
        if (storage) storage.setItem(key, value);
      } catch {
        /* ignore */
      }
    },
    remove(key) {
      try {
        if (storage) storage.removeItem(key);
      } catch {
        /* ignore */
      }
    },
  };
}

class ApiError extends Error {
  constructor(kind, status = 0) {
    super(kind);
    this.kind = kind;
    this.status = status;
  }
}

function classify(status) {
  if (status === 429) return "rate";
  if (status === 503) return "unavailable";
  if (status === 409) return "busy";
  if (status === 404) return "expired";
  if (status === 413 || status === 422) return "rejected";
  return "generic";
}

function createApi({ base, key, fetchImpl }) {
  const root = `${base}/api/public/chat/${encodeURIComponent(key)}`;

  async function request(path, { method = "GET", body, token } = {}) {
    const controller = typeof AbortController === "function" ? new AbortController() : null;
    const timer = controller ? setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS) : null;
    let response;
    try {
      response = await fetchImpl(`${root}${path}`, {
        method,
        mode: "cors",
        credentials: "omit", // never send the host page's cookies
        headers: {
          ...(body ? { "Content-Type": "application/json" } : {}),
          ...(token ? { "X-Conversation-Token": token } : {}),
        },
        body: body ? JSON.stringify(body) : undefined,
        signal: controller ? controller.signal : undefined,
      });
    } catch {
      throw new ApiError("network");
    } finally {
      if (timer) clearTimeout(timer);
    }
    if (!response.ok) throw new ApiError(classify(response.status), response.status);
    try {
      return await response.json();
    } catch {
      throw new ApiError("generic", response.status);
    }
  }

  return {
    config: () => request("/config"),
    createConversation: (visitorId) =>
      request("/conversations", { method: "POST", body: visitorId ? { visitor_id: visitorId } : {} }),
    sendMessage: (conversationId, token, content) =>
      request("/messages", {
        method: "POST",
        body: { conversation_id: conversationId, content },
        token,
      }),
    getConversation: (conversationId, token) => request(`/conversations/${encodeURIComponent(conversationId)}`, { token }),
    captureLead: (conversationId, token, contact) => request("/leads", { method: "POST", body: { conversation_id: conversationId, ...contact }, token }),
  };
}

function el(doc, tag, className, attrs = {}) {
  const node = doc.createElement(tag);
  if (className) node.className = className;
  for (const [name, value] of Object.entries(attrs)) node.setAttribute(name, value);
  return node;
}

// The state that makes "one widget per page" true lives on the PAGE (globalThis), not in this
// module: the same bundle can be loaded more than once (a snippet pasted twice, or a dashboard
// effect that React runs twice in development and that appends the script each time), and every
// copy has its own module scope. Starting also waits on the network (the config, then any saved
// conversation), and a page can be left or re-rendered in that time, so:
//   * `generation` is bumped by destroy(): a start that is still waiting notices it was
//     cancelled and mounts nothing (no stray launcher on a page that was left);
//   * `starting` stops a second start from racing the first.
// Without all of that a widget could appear after its page was gone, or twice.
const page = typeof globalThis !== "undefined" ? globalThis : window;
if (!page.__bizflowChat) page.__bizflowChat = { host: null, generation: 0, starting: false };
const shared = page.__bizflowChat;

export function destroy() {
  shared.generation += 1;
  shared.starting = false;
  if (shared.host && shared.host.parentNode) shared.host.parentNode.removeChild(shared.host);
  shared.host = null;
}

/**
 * Starts the widget. Resolves to the controller, or null if the chat is unavailable for this
 * key (unknown/disabled key, misconfiguration, backend unreachable) or another start is already
 * under way or done: in that case nothing is rendered on the host page.
 */
export async function init(options = {}) {
  if ((shared.host && shared.host.isConnected) || shared.starting) return null; // one widget per page
  shared.starting = true;
  const ticket = shared.generation;
  try {
    return await mount(options, () => ticket === shared.generation);
  } finally {
    if (ticket === shared.generation) shared.starting = false;
  }
}

async function mount(options, isCurrent) {
  const win = options.window || (typeof window !== "undefined" ? window : null);
  const doc = options.document || (win && win.document);
  const fetchImpl = options.fetch || (win && win.fetch && win.fetch.bind(win));
  if (!doc || !fetchImpl) return null;

  const key = String(options.key || "");
  const base = String(options.api || "").replace(/\/+$/, "");
  if (!KEY_RE.test(key) || !/^https?:\/\//i.test(base)) return null;

  const local = safeStorage(options.localStorage ?? (win ? win.localStorage : null));
  const session = safeStorage(options.sessionStorage ?? (win ? win.sessionStorage : null));
  const sessionKey = SESSION_STORAGE_PREFIX + key;
  const api = createApi({ base, key, fetchImpl });

  let config;
  try {
    config = await api.config();
  } catch {
    return null;
  }
  if (!isCurrent()) return null; // the page was left (destroy) while the config was loading

  const title = String(options.title || config.business_name || "Chat with us");
  const welcome = String(config.welcome_message || "Hi! How can I help you today?");

  // ---- DOM (static structure only; no HTML strings) ----
  const host = el(doc, "div", "", { id: "bizflow-chat-host" });
  const shadow = host.attachShadow({ mode: "open" });
  const style = doc.createElement("style");
  style.textContent = STYLES;
  const root = el(doc, "div", "bf-root");

  const launcher = el(doc, "button", "bf-launcher", {
    type: "button",
    "aria-label": "Open chat",
    "aria-expanded": "false",
  });
  launcher.textContent = "\u{1F4AC}";

  const panel = el(doc, "section", "bf-panel", { role: "dialog", "aria-label": title, hidden: "" });
  const header = el(doc, "header", "bf-header");
  const heading = el(doc, "span", "bf-title");
  heading.textContent = title;
  const close = el(doc, "button", "bf-close", { type: "button", "aria-label": "Close chat" });
  close.textContent = "×";
  header.append(heading, close);

  const log = el(doc, "div", "bf-log", { role: "log", "aria-live": "polite", "aria-label": "Conversation" });
  const status = el(doc, "div", "bf-status", { role: "status" });
  const form = el(doc, "form", "bf-form");
  const input = el(doc, "textarea", "bf-input", {
    rows: "1",
    maxlength: String(MAX_MESSAGE_CHARS),
    placeholder: "Type your message...",
    "aria-label": "Your message",
  });
  const send = el(doc, "button", "bf-send", { type: "submit" });
  send.textContent = "Send";
  form.append(input, send);

  const leadToggle = el(doc, "button", "bf-lead-toggle", { type: "button" });
  leadToggle.textContent = "Leave contact details (optional)";
  const leadForm = el(doc, "form", "bf-lead-form", { hidden: "" });
  const leadName = el(doc, "input", "bf-lead-input", { name: "name", maxlength: "255", placeholder: "Name (optional)", "aria-label": "Name (optional)" });
  const leadEmail = el(doc, "input", "bf-lead-input", { name: "email", maxlength: "320", type: "email", placeholder: "Email", "aria-label": "Email" });
  const leadPhone = el(doc, "input", "bf-lead-input", { name: "phone", maxlength: "50", type: "tel", placeholder: "Phone", "aria-label": "Phone" });
  const consentLabel = el(doc, "label", "bf-consent");
  const consent = el(doc, "input", "", { name: "marketing_consent", type: "checkbox" });
  const consentText = doc.createElement("span");
  consentText.textContent = "I agree to receive follow-up messages";
  consentLabel.append(consent, consentText);
  const leadSubmit = el(doc, "button", "bf-lead-send", { type: "submit" });
  leadSubmit.textContent = "Share details";
  leadForm.append(leadName, leadEmail, leadPhone, consentLabel, leadSubmit);

  panel.append(header, log, status, leadToggle, leadForm, form);
  root.append(launcher, panel);
  shadow.append(style, root);

  // ---- state ----
  let conversation = null; // { id, token }
  let busy = false;
  let started = false;
  let lastText = "";

  function scrollToEnd() {
    log.scrollTop = log.scrollHeight;
  }

  function addMessage(role, text) {
    const bubble = el(doc, "div", `bf-msg bf-${role}`);
    bubble.textContent = text; // text only: never HTML
    log.appendChild(bubble);
    scrollToEnd();
    return bubble;
  }

  function setBusy(value) {
    busy = value;
    input.disabled = value;
    send.disabled = value;
    log.setAttribute("aria-busy", String(value));
    status.textContent = value ? "Assistant is typing..." : "";
    const existing = log.querySelector(".bf-typing");
    if (value && !existing) {
      const dots = el(doc, "div", "bf-msg bf-assistant bf-typing", { "aria-hidden": "true" });
      dots.textContent = "...";
      log.appendChild(dots);
      scrollToEnd();
    } else if (!value && existing) {
      existing.remove();
    }
  }

  function showError(kind) {
    const box = el(doc, "div", "bf-msg bf-error", { role: "alert" });
    const message = doc.createElement("span");
    message.textContent = ERRORS[kind] || ERRORS.generic;
    const retry = el(doc, "button", "bf-retry", { type: "button" });
    retry.textContent = "Try again";
    retry.addEventListener("click", () => {
      box.remove();
      submit(lastText, { echo: false });
    });
    box.append(message, retry);
    log.appendChild(box);
    scrollToEnd();
  }

  function remember() {
    if (conversation) session.set(sessionKey, JSON.stringify(conversation));
    else session.remove(sessionKey);
  }

  function showWelcome() {
    if (!started) {
      started = true;
      addMessage("assistant", welcome);
    }
  }

  async function restore() {
    const raw = session.get(sessionKey);
    if (!raw) return;
    let saved;
    try {
      saved = JSON.parse(raw);
    } catch {
      session.remove(sessionKey);
      return;
    }
    if (!saved || typeof saved.id !== "string" || typeof saved.token !== "string") {
      session.remove(sessionKey);
      return;
    }
    try {
      const data = await api.getConversation(saved.id, saved.token);
      conversation = saved;
      started = true;
      addMessage("assistant", welcome);
      for (const m of data.messages || []) addMessage(m.role === "user" ? "user" : "assistant", String(m.content));
    } catch {
      session.remove(sessionKey); // expired or invalid: start fresh
    }
  }

  async function ensureConversation() {
    if (conversation) return;
    let visitor = local.get(VISITOR_STORAGE_KEY);
    if (!visitor) {
      visitor = randomId();
      local.set(VISITOR_STORAGE_KEY, visitor);
    }
    const created = await api.createConversation(visitor);
    conversation = { id: created.conversation_id, token: created.token };
    remember();
  }

  async function submit(rawText, { echo = true } = {}) {
    const text = String(rawText).trim();
    if (!text || busy) return;
    if (text.length > MAX_MESSAGE_CHARS) {
      status.textContent = `Messages can be at most ${MAX_MESSAGE_CHARS} characters.`;
      return;
    }
    lastText = text;
    if (echo) addMessage("user", text);
    setBusy(true);
    try {
      await ensureConversation();
      const reply = await api.sendMessage(conversation.id, conversation.token, text);
      addMessage("assistant", String(reply.message.content));
    } catch (error) {
      const kind = error instanceof ApiError ? error.kind : "generic";
      if (kind === "expired") {
        conversation = null; // the server no longer knows this conversation
        remember();
      }
      showError(kind);
    } finally {
      setBusy(false);
      if (!input.disabled) input.focus();
    }
  }

  // ---- events ----
  function setOpen(open) {
    panel.hidden = !open;
    launcher.setAttribute("aria-expanded", String(open));
    launcher.setAttribute("aria-label", open ? "Close chat" : "Open chat");
    if (open) {
      showWelcome();
      input.focus();
    } else {
      launcher.focus();
    }
  }

  launcher.addEventListener("click", () => setOpen(panel.hidden));
  close.addEventListener("click", () => setOpen(false));
  panel.addEventListener("keydown", (event) => {
    if (event.key === "Escape") setOpen(false);
  });
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const text = input.value;
    if (!text.trim() || busy) return;
    input.value = "";
    submit(text);
  });
  leadToggle.addEventListener("click", () => {
    leadForm.hidden = !leadForm.hidden;
    if (!leadForm.hidden) leadEmail.focus();
  });
  leadForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (busy) return;
    leadSubmit.disabled = true;
    status.textContent = "Saving your contact details...";
    try {
      await ensureConversation();
      await api.captureLead(conversation.id, conversation.token, {
        name: leadName.value.trim() || null,
        email: leadEmail.value.trim() || null,
        phone: leadPhone.value.trim() || null,
        marketing_consent: consent.checked,
      });
      leadForm.hidden = true;
      status.textContent = "Thanks. Your contact details were saved.";
      leadName.value = leadEmail.value = leadPhone.value = "";
      consent.checked = false;
    } catch (error) {
      const kind = error instanceof ApiError ? error.kind : "generic";
      status.textContent = kind === "rejected" ? "Enter a valid email or phone number." : ERRORS[kind] || ERRORS.generic;
    } finally {
      leadSubmit.disabled = false;
    }
  });
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      form.requestSubmit ? form.requestSubmit() : form.dispatchEvent(new win.Event("submit", { cancelable: true }));
    }
  });

  await restore();
  if (!isCurrent()) return null; // ...or while a saved conversation was loading
  (doc.body || doc.documentElement).appendChild(host);
  shared.host = host;

  return { host, open: () => setOpen(true), close: () => setOpen(false), destroy };
}

// Auto-start when loaded through a <script data-key="..." data-api="..."> tag.
if (typeof document !== "undefined" && document.currentScript) {
  const script = document.currentScript;
  const { key, api, title } = script.dataset || {};
  if (key) {
    const start = () => init({ key, api: api || new URL(script.src).origin, title });
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
    else start();
  }
}
