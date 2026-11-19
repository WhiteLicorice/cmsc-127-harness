import { spawnSync } from "node:child_process";
import { createServer, type Server } from "node:http";
import { existsSync } from "node:fs";
import { readFile } from "node:fs/promises";
import { extname, resolve } from "node:path";
import { chromium, type Browser, type Page } from "playwright";
import { afterAll, beforeAll, describe, expect, it } from "vitest";

const root = resolve(import.meta.dirname, "../..");
const publicRoot = resolve(root, "public");
const HOSTED_ORIGIN = "https://whitelicorice.github.io";
const HOSTED_PAGE = `${HOSTED_ORIGIN}/cmsc-127-harness/`;
const SERVER = "http://127.0.0.1:55440";

const TYPES: Record<string, string> = {
  ".html": "text/html", ".css": "text/css", ".js": "text/javascript", ".txt": "text/plain",
};

// The page files, with the asset root standing in for whichever origin served
// them. A hosted load and a local load read the same bytes.
async function pageFile(pathname: string): Promise<{ body: Buffer; type: string } | null> {
  const relative = pathname === "/" || pathname === "/cmsc-127-harness/"
    ? "index.html"
    : pathname.replace(/^\/cmsc-127-harness\//, "/").slice(1);
  const file = resolve(publicRoot, relative);
  if (!file.startsWith(publicRoot)) {
    return null;
  }
  try {
    return { body: await readFile(file), type: TYPES[extname(file)] ?? "application/octet-stream" };
  } catch {
    return null;
  }
}

const STATUS_OK = {
  server: "running", databaseStatus: "connected", api_version: 1,
  database: "campus", role: "ada", lastPoll: "2026-11-19T09:00:00Z",
};
const TABLES_OK = {
  schema: "public", schemas: ["public"], message: "",
  tables: [{ name: "student", kind: "r", primaryKey: ["id"], rows: [{ id: 1, name: "Ana" }] }],
};

function apiBody(pathname: string): unknown {
  if (pathname === "/api/status") return STATUS_OK;
  if (pathname === "/api/changes") return { pollIntervalMs: 1000, feedLimit: 100 };
  if (pathname === "/api/tables") return TABLES_OK;
  if (pathname === "/api/sessions") return { sessions: [] };
  if (pathname === "/api/checks") return { results: [], passed: 0, failed: 0, skipped: 0 };
  if (pathname === "/api/roles") return { schema: "public", privileges: [] };
  if (pathname === "/api/schema") return { schema: "public", tables: [], differences: [], relationships: [] };
  return { message: "unknown" };
}

let server: Server;
let browser: Browser;
let baseUrl = "";

// The page under test is the compiled one. A workflow runs the tests before
// the build, so compile the page here when it is absent.
function ensureBuilt(): void {
  if (existsSync(resolve(publicRoot, "app.js"))) {
    return;
  }
  const compiler = resolve(root, "node_modules", "typescript", "bin", "tsc");
  const built = spawnSync(process.execPath, [compiler, "-p", root], { stdio: "inherit" });
  if (built.status !== 0) {
    throw new Error("The page did not compile.");
  }
}

async function fulfilPage(route: import("playwright").Route, url: string): Promise<void> {
  const file = await pageFile(new URL(url).pathname);
  if (file === null) {
    await route.fulfill({ status: 404, body: "missing" });
    return;
  }
  await route.fulfill({ status: 200, contentType: file.type, body: file.body });
}

beforeAll(async () => {
  ensureBuilt();
  server = createServer((request, response) => {
    void pageFile(request.url ?? "/").then((file) => {
      if (file === null) {
        response.writeHead(404);
        response.end();
        return;
      }
      response.writeHead(200, { "Content-Type": file.type });
      response.end(file.body);
    });
  });
  await new Promise<void>((ready) => server.listen(0, "127.0.0.1", ready));
  const address = server.address();
  if (address === null || typeof address === "string") {
    throw new Error("The hosted-mode test server did not bind to a port.");
  }
  baseUrl = `http://127.0.0.1:${address.port}/`;
  browser = await chromium.launch({ headless: true });
}, 60_000);

afterAll(async () => {
  await browser?.close();
  await new Promise<void>((closed) => server?.close(() => closed()));
});

async function hostedPage(status: unknown): Promise<{ page: Page; requested: string[] }> {
  const context = await browser.newContext();
  const page = await context.newPage();
  const requested: string[] = [];
  await page.route(`${HOSTED_ORIGIN}/**`, (route) => fulfilPage(route, route.request().url()));
  await page.route(`${SERVER}/api/**`, (route) => {
    const url = new URL(route.request().url());
    requested.push(route.request().url());
    const body = url.pathname === "/api/status" ? status : apiBody(url.pathname);
    return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
  });
  await page.goto(HOSTED_PAGE, { waitUntil: "domcontentloaded" });
  await page.waitForFunction(
    () => document.querySelectorAll("#panels button").length === 7,
    undefined,
    { timeout: 15_000 },
  );
  return { page, requested };
}

describe("local page", () => {
  it("registers no worker and keeps every request on its own origin", async () => {
    const page = await browser.newPage();
    const requested: string[] = [];
    page.on("request", (request) => requested.push(request.url()));
    await page.goto(baseUrl, { waitUntil: "domcontentloaded" });
    await page.waitForFunction(
      () => document.querySelectorAll("#panels button").length === 7,
      undefined,
      { timeout: 15_000 },
    );
    expect(await page.evaluate(() => navigator.serviceWorker.getRegistrations().then((list) => list.length)))
      .toBe(0);
    const offOrigin = requested.filter((url) => !url.startsWith(baseUrl));
    expect(offOrigin).toEqual([]);
    await page.context().close();
  }, 60_000);
});

describe("hosted page", () => {
  it("waits for the Connect click before it polls", async () => {
    const { page, requested } = await hostedPage(STATUS_OK);
    const status = page.locator("#panel-content");
    expect(await status.textContent()).toContain(
      "Start the harness page server on your computer, then click Connect.",
    );
    expect(await page.getByRole("button", { name: "Connect", exact: true }).count()).toBe(1);
    expect(requested).toEqual([]);
    await page.context().close();
  }, 60_000);

  it("polls the loopback server after the click", async () => {
    const { page, requested } = await hostedPage(STATUS_OK);
    await page.getByRole("button", { name: "Connect", exact: true }).click();
    await page.waitForFunction(
      () => (document.querySelector("#poll-summary")?.textContent ?? "").startsWith("Last table poll:"),
      undefined,
      { timeout: 15_000 },
    );
    expect(requested).toContain(`${SERVER}/api/status`);
    expect(requested.some((url) => url.startsWith(`${SERVER}/api/tables`))).toBe(true);
    await page.context().close();
  }, 60_000);

  it("draws one plain line for a server older than the minimum", async () => {
    const tooOld = { server: "running", databaseStatus: "connected" };
    const { page, requested } = await hostedPage(tooOld);
    await page.getByRole("button", { name: "Connect", exact: true }).click();
    await page.waitForFunction(
      (text) => (document.querySelector("#panel-content")?.textContent ?? "").includes(text),
      "This harness server is too old for the hosted page.",
      { timeout: 15_000 },
    );
    const text = await page.locator("#panel-content").textContent();
    expect(text).toBe(
      "This harness server is too old for the hosted page. Use the local page that the server prints.",
    );
    expect(requested.filter((url) => url.includes("/api/tables"))).toEqual([]);
    await page.context().close();
  }, 60_000);

  it("reports a missing server in one line", async () => {
    const { page } = await hostedPage(STATUS_OK);
    await page.unroute(`${SERVER}/api/**`);
    await page.route(`${SERVER}/api/**`, (route) => route.abort());
    await page.getByRole("button", { name: "Connect", exact: true }).click();
    await page.waitForFunction(
      () => {
        const message = document.querySelector("#connection-message");
        return message !== null && !message.hasAttribute("hidden")
          && (message.textContent ?? "").includes("The page can't reach the harness server");
      },
      undefined,
      { timeout: 15_000 },
    );
    expect(await page.locator("#connection-message").textContent()).toBe(
      `The page can't reach the harness server at ${SERVER}. Start it, allow the browser's local network prompt, then click Connect.`,
    );
    await page.context().close();
  }, 60_000);
});
