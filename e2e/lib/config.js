// Shared settings for every browser test. Everything can be overridden from the environment so the
// same scripts run against the stack that run.mjs starts, or against a stack you started yourself.
const fs = require("fs");
const path = require("path");
const { execFileSync } = require("child_process");

const env = process.env;

const config = {
  BASE: env.E2E_BASE || "http://localhost:3000", // the dashboard
  API: env.E2E_API || "http://localhost:8000", // the FastAPI backend
  FAKE: env.E2E_FAKE || "http://127.0.0.1:9400", // the fake Shopify (tests/shopify/http_server.py)
  EMBED_PORT: Number(env.E2E_EMBED_PORT || 5173), // chat.spec.js serves a third-party page here
  // The Shopify app credentials the fake accepts; run.mjs starts the API with the same values.
  SHOP: env.E2E_SHOP || "e2e-shop.myshopify.com",
  CLIENT_SECRET: env.E2E_SHOPIFY_CLIENT_SECRET || "e2e-client-secret",
  // Where screenshots and traces go. Git-ignored; never commit them (they can show customer data).
  ARTIFACTS: env.E2E_ARTIFACTS || path.join(__dirname, "..", "artifacts"),
  // psql access for assertions about what is (not) in the database.
  DB_CONTAINER: env.E2E_DB_CONTAINER || "product-db-1",
  DB_NAME: env.E2E_DB_NAME || "bizflow_e2e",
  DB_USER: env.E2E_DB_USER || "bizflow",
};

fs.mkdirSync(config.ARTIFACTS, { recursive: true });

/** Run one SQL statement in the end-to-end database and return the trimmed output. */
config.sql = function sql(query) {
  return execFileSync(
    "docker",
    [
      "exec",
      config.DB_CONTAINER,
      "psql",
      "-U",
      config.DB_USER,
      "-d",
      config.DB_NAME,
      "-At",
      "-c",
      query,
    ],
    { encoding: "utf8" },
  ).trim();
};

/**
 * Launch a headless browser without downloading one. Order: E2E_BROWSER_CHANNEL if set, then the
 * bundled Chromium (after `npx playwright install chromium`), then installed Chrome, then Edge.
 */
config.launchBrowser = async function launchBrowser() {
  const { chromium } = require("playwright-core");
  const attempts = [];
  if (env.E2E_BROWSER_CHANNEL) attempts.push({ channel: env.E2E_BROWSER_CHANNEL });
  else attempts.push({}, { channel: "chrome" }, { channel: "msedge" });
  const errors = [];
  for (const options of attempts) {
    try {
      return await chromium.launch({ headless: true, ...options });
    } catch (error) {
      errors.push(`${options.channel || "bundled chromium"}: ${String(error.message).split("\n")[0]}`);
    }
  }
  throw new Error(
    "No browser could be launched. Install one (`npx playwright install chromium`) or set " +
      `E2E_BROWSER_CHANNEL=chrome|msedge.\n  ${errors.join("\n  ")}`,
  );
};

module.exports = config;
