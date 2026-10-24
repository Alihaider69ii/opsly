#!/usr/bin/env node
// One command that starts the whole stack in an isolated database, runs the browser tests against it
// and tears everything down:
//
//   cd e2e && npm ci && npm test                 offline suite (no internet needed)
//   npm run test:full                            + knowledge and chat suites (crawl real public sites)
//   node run.mjs auth shopify                    only the named specs
//   node run.mjs --keep                          leave the stack running afterwards (debugging)
//
// What it starts (all on localhost): a fake Shopify (tests/shopify/http_server.py), the API, the
// background worker and the dashboard in development mode. The database `bizflow_e2e` and Redis db 14
// are created for the run and removed at the end; the development database is never touched.
// Every secret is generated for the run and exists only in the child processes' environment.
import { execFileSync, spawn, spawnSync } from "node:child_process";
import crypto from "node:crypto";
import fs from "node:fs";
import net from "node:net";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, "..");
const BACKEND = path.join(ROOT, "backend");
const FRONTEND = path.join(ROOT, "frontend");
const IS_WINDOWS = process.platform === "win32";
const PYTHON = path.join(BACKEND, ".venv", IS_WINDOWS ? "Scripts/python.exe" : "bin/python");

const args = process.argv.slice(2);
const flags = new Set(args.filter((a) => a.startsWith("--")));
const requested = args.filter((a) => !a.startsWith("--"));

const OFFLINE = ["auth", "security", "layout", "shopify"];
const INTERNET = ["knowledge", "chat"]; // crawl example.com / quotes.toscrape.com
const suites = requested.length ? requested : flags.has("--with-internet") ? [...OFFLINE, ...INTERNET] : OFFLINE;

const stamp = new Date().toISOString().replace(/[:.]/g, "-");
const ARTIFACTS = process.env.E2E_ARTIFACTS || path.join(HERE, "artifacts", stamp);
fs.mkdirSync(path.join(ARTIFACTS, "logs"), { recursive: true });

const PORTS = { frontend: 3000, api: 8000, fake: 9400, embed: 5173 };
const DB_NAME = "bizflow_e2e";
const REDIS_DB = 14;
const children = [];

const log = (msg) => console.log(`[e2e] ${msg}`);
const fail = (msg) => {
  console.error(`[e2e] ERROR: ${msg}`);
  throw new Error(msg);
};

function run(cmd, cmdArgs, options = {}) {
  return spawnSync(cmd, cmdArgs, { encoding: "utf8", ...options });
}

function composeContainer(service) {
  const result = run("docker", ["compose", "-f", path.join(ROOT, "docker-compose.yml"), "ps", "-q", service], { cwd: ROOT });
  const id = (result.stdout || "").trim().split(/\s+/)[0];
  if (!id) fail(`the "${service}" container is not running. Start it: docker compose up -d db redis`);
  return id;
}

function portFree(port) {
  return new Promise((resolve) => {
    const server = net.createServer();
    server.once("error", () => resolve(false));
    server.once("listening", () => server.close(() => resolve(true)));
    server.listen(port, "127.0.0.1");
  });
}

async function waitFor(url, label, timeoutMs = 180000, accept = (r) => r.status < 500) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(url, { redirect: "manual", signal: AbortSignal.timeout(5000) });
      if (accept(response)) return;
    } catch {
      /* not up yet */
    }
    await new Promise((r) => setTimeout(r, 1000));
  }
  fail(`${label} did not become ready at ${url} within ${timeoutMs / 1000}s (see ${path.join(ARTIFACTS, "logs")})`);
}

function start(name, command, commandArgs, { cwd, env }) {
  const out = fs.openSync(path.join(ARTIFACTS, "logs", `${name}.log`), "a");
  // .cmd files cannot be spawned directly on Windows; run them through cmd.exe explicitly.
  const viaCmd = IS_WINDOWS && command.endsWith(".cmd");
  const child = spawn(viaCmd ? "cmd.exe" : command, viaCmd ? ["/d", "/s", "/c", command, ...commandArgs] : commandArgs, {
    cwd,
    env: { ...process.env, ...env },
    stdio: ["ignore", out, out],
    detached: !IS_WINDOWS,
    windowsHide: true,
  });
  child.on("error", (error) => console.error(`[e2e] ${name} failed to start: ${error.message}`));
  children.push({ name, child });
  return child;
}

function stopAll() {
  for (const { name, child } of children.reverse()) {
    if (child.exitCode !== null || !child.pid) continue;
    try {
      if (IS_WINDOWS) run("taskkill", ["/pid", String(child.pid), "/T", "/F"]);
      else process.kill(-child.pid, "SIGTERM");
    } catch {
      /* already gone */
    }
    log(`stopped ${name}`);
  }
}

function secret(bytes) {
  return crypto.randomBytes(bytes).toString("base64url");
}

async function preflight() {
  if (!fs.existsSync(PYTHON)) fail(`backend virtualenv not found (${PYTHON}). Run: cd backend && python -m venv .venv && .venv/Scripts/pip install -r requirements-dev.txt`);
  if (!fs.existsSync(path.join(FRONTEND, "node_modules"))) fail("frontend dependencies missing. Run: cd frontend && npm ci");
  if (!fs.existsSync(path.join(HERE, "node_modules", "playwright-core"))) fail("e2e dependencies missing. Run: cd e2e && npm ci");
  const widget = path.join(FRONTEND, "public", "widget.js");
  if (!fs.existsSync(widget)) fail("frontend/public/widget.js is missing. Run: cd widget && npm ci && npm run build");
  for (const name of suites) {
    if (!fs.existsSync(path.join(HERE, `${name}.spec.js`))) fail(`unknown suite "${name}" (known: ${[...OFFLINE, ...INTERNET].join(", ")})`);
  }
  for (const [what, port] of Object.entries(PORTS)) {
    if (what === "embed" && !suites.includes("chat")) continue;
    if (!(await portFree(port))) fail(`port ${port} (${what}) is in use. Stop whatever runs there, or this run would test something else.`);
  }
}

function resetDatabase(dbContainer, redisContainer) {
  const psql = (db, sql) => execFileSync("docker", ["exec", dbContainer, "psql", "-U", "bizflow", "-d", db, "-v", "ON_ERROR_STOP=1", "-c", sql], { encoding: "utf8" });
  psql("postgres", `DROP DATABASE IF EXISTS ${DB_NAME} WITH (FORCE)`);
  psql("postgres", `CREATE DATABASE ${DB_NAME}`);
  execFileSync("docker", ["exec", redisContainer, "redis-cli", "-n", String(REDIS_DB), "flushdb"], { encoding: "utf8" });
}

async function main() {
  await preflight();
  const dbContainer = composeContainer("db");
  const redisContainer = composeContainer("redis");
  process.env.E2E_DB_CONTAINER = dbContainer;
  process.env.E2E_DB_NAME = DB_NAME;
  process.env.E2E_ARTIFACTS = ARTIFACTS;
  log(`artifacts: ${ARTIFACTS}`);

  log(`creating database ${DB_NAME} and applying migrations`);
  resetDatabase(dbContainer, redisContainer);
  const databaseUrl = `postgresql+psycopg://bizflow:bizflow@127.0.0.1:5432/${DB_NAME}`;

  const shopifyClientSecret = process.env.E2E_SHOPIFY_CLIENT_SECRET || "e2e-client-secret";
  const backendEnv = {
    ENVIRONMENT: "development",
    DATABASE_URL: databaseUrl,
    REDIS_URL: `redis://127.0.0.1:6379/${REDIS_DB}`,
    REDIS_QUEUE_KEY: "bizflow:e2e:queue",
    JWT_SECRET_KEY: secret(48),
    SECRET_ENCRYPTION_KEY: crypto.randomBytes(32).toString("base64url"),
    LLM_PROVIDER: "fake",
    EMBEDDING_PROVIDER: "fake",
    CORS_ORIGINS: `http://localhost:${PORTS.frontend}`,
    FORWARDED_ALLOW_IPS: "127.0.0.1",
    RATE_LIMIT_BACKEND: "memory",
    AUTH_RATE_LIMIT_ATTEMPTS: "500", // many sign-ups from one address; the public chat limits stay default
    CRAWL_MIN_INTERVAL_SECONDS: "0", // the knowledge suite re-crawls straight away (the default is 60 s)
    CRAWL_MAX_PAGES: "8", // the knowledge suite asserts the worker stops a multi-page site at 8 pages
    SHOPIFY_CLIENT_ID: "e2e-client-id",
    SHOPIFY_CLIENT_SECRET: shopifyClientSecret,
    SHOPIFY_REDIRECT_URI: `http://localhost:${PORTS.frontend}/integrations/shopify/callback`,
    SHOPIFY_DEV_BASE_URL: `http://127.0.0.1:${PORTS.fake}`,
    SHOPIFY_WEBHOOK_BASE_URL: `http://localhost:${PORTS.api}`,
    SHOPIFY_SYNC_MIN_INTERVAL_SECONDS: "3",
    WORKER_HEARTBEAT_FILE: path.join(ARTIFACTS, "worker-heartbeat"),
    PYTHONUNBUFFERED: "1",
    PYTHONIOENCODING: "utf-8",
  };

  const migrate = run(PYTHON, ["-m", "alembic", "upgrade", "head"], { cwd: BACKEND, env: { ...process.env, ...backendEnv } });
  if (migrate.status !== 0) fail(`migrations failed:\n${migrate.stdout}\n${migrate.stderr}`);

  log("starting fake Shopify, API, worker and dashboard");
  start("fake-shopify", PYTHON, ["-m", "tests.shopify.http_server", "--port", String(PORTS.fake), "--client-secret", shopifyClientSecret], { cwd: BACKEND, env: { PYTHONUNBUFFERED: "1" } });
  start("api", PYTHON, ["-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", String(PORTS.api), "--proxy-headers"], { cwd: BACKEND, env: backendEnv });
  start("worker", PYTHON, ["-m", "app.workers.main"], { cwd: BACKEND, env: backendEnv });
  start("frontend", IS_WINDOWS ? "npm.cmd" : "npm", ["run", "dev", "--", "-p", String(PORTS.frontend)], {
    cwd: FRONTEND,
    env: {
      API_URL: `http://localhost:${PORTS.api}`,
      NEXT_PUBLIC_API_URL: `http://localhost:${PORTS.api}`,
      NEXT_PUBLIC_APP_URL: `http://localhost:${PORTS.frontend}`,
      NEXT_TELEMETRY_DISABLED: "1",
    },
  });

  await waitFor(`http://127.0.0.1:${PORTS.fake}/__control/state`, "fake Shopify", 60000, (r) => r.status === 200);
  await waitFor(`http://localhost:${PORTS.api}/health/ready`, "API", 90000, (r) => r.status === 200);
  await waitFor(`http://localhost:${PORTS.api}/health/worker`, "worker", 90000, (r) => r.status === 200);
  await waitFor(`http://localhost:${PORTS.frontend}/login`, "dashboard", 240000, (r) => r.status === 200);
  log("stack is ready; warming the dashboard's pages");
  for (const route of ["/signup", "/onboarding", "/dashboard", "/knowledge", "/website-chat", "/integrations/shopify"]) {
    await fetch(`http://localhost:${PORTS.frontend}${route}`, { redirect: "manual", signal: AbortSignal.timeout(120000) }).catch(() => {});
  }

  const results = [];
  for (const name of suites) {
    log(`--- ${name} ---------------------------------------------------------`);
    const started = Date.now();
    const result = spawnSync(process.execPath, [path.join(HERE, `${name}.spec.js`)], {
      cwd: HERE,
      stdio: "inherit",
      env: {
        ...process.env,
        E2E_BASE: `http://localhost:${PORTS.frontend}`,
        E2E_API: `http://localhost:${PORTS.api}`,
        E2E_FAKE: `http://127.0.0.1:${PORTS.fake}`,
        E2E_SHOPIFY_CLIENT_SECRET: shopifyClientSecret,
      },
    });
    results.push({ name, code: result.status, seconds: Math.round((Date.now() - started) / 1000) });
  }

  console.log("\n[e2e] summary");
  for (const r of results) console.log(`  ${r.code === 0 ? "PASS" : "FAIL"}  ${r.name.padEnd(10)} ${r.seconds}s`);
  return results.every((r) => r.code === 0);
}

let ok = false;
try {
  ok = await main();
} catch (error) {
  console.error(`[e2e] aborted: ${error.message}`);
} finally {
  if (flags.has("--keep")) {
    log("--keep: leaving the stack running; stop it yourself (and drop database bizflow_e2e)");
  } else {
    stopAll();
    try {
      const dbContainer = composeContainer("db");
      execFileSync("docker", ["exec", dbContainer, "psql", "-U", "bizflow", "-d", "postgres", "-c", `DROP DATABASE IF EXISTS ${DB_NAME} WITH (FORCE)`], { encoding: "utf8" });
      execFileSync("docker", ["exec", composeContainer("redis"), "redis-cli", "-n", String(REDIS_DB), "flushdb"], { encoding: "utf8" });
      log(`removed database ${DB_NAME} and Redis db ${REDIS_DB}`);
    } catch {
      /* best effort */
    }
  }
}
process.exit(ok ? 0 : 1);
