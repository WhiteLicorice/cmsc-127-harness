import { spawnSync } from "node:child_process";
import { createServer, type Server } from "node:http";
import { readFile } from "node:fs/promises";
import { extname, resolve } from "node:path";
import { chromium, type Browser, type Page } from "playwright";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { closeServer } from "./listen";

const root = resolve(import.meta.dirname, "../..");
const publicRoot = resolve(root, "public");
const HOSTED_ORIGIN = "https://whitelicorice.github.io";
const HOSTED_PAGE = `${HOSTED_ORIGIN}/cmsc-127-harness/`;
const SERVER = "http://127.0.0.1:55440";

const TYPES: Record<string, string> = {
  ".html": "text/html", ".css": "text/css", ".js": "text/javascript", ".txt": "text/plain",
};

// The page files, with the asset root standing in for whichever origin served
// them. A hosted load and a local load read the same bytes. The worker script
// stands in for the offline lane's worker, so a registration that happens
// installs and becomes visible to the test.
const WORKER_SCRIPT = "self.addEventListener('install', () => self.skipWaiting());\n";

async function pageFile(pathname: string): Promise<{ body: Buffer; type: string } | null> {
  if (pathname === "/service-worker.js") {
    return { body: Buffer.from(WORKER_SCRIPT, "utf-8"), type: "text/javascript" };
  }
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

// The page under test is the compiled one. Compile it here every time, so a
// stale `public/app.js` cannot answer for the source in `src/`.
function ensureBuilt(): void {
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
  await closeServer(server);
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

// A stand-in for another local program on the server port. It answers with an
// HTML page and the permissive CORS header, so the browser reads the body and
// the JSON read fails.
async function htmlServer(): Promise<{ page: Page; port: number; close: () => Promise<void> }> {
  const stand = createServer((_request, response) => {
    response.writeHead(200, {
      "Content-Type": "text/html; charset=utf-8",
      "Access-Control-Allow-Origin": "*",
    });
    response.end("<!doctype html><title>Another program</title><p>Not the harness.</p>");
  });
  await new Promise<void>((ready) => stand.listen(0, "127.0.0.1", ready));
  const address = stand.address();
  if (address === null || typeof address === "string") {
    throw new Error("The stand-in server did not bind to a port.");
  }
  const context = await browser.newContext();
  // Chrome asks for loopback access before an HTTPS page may reach a local
  // port. Grant it, so the request reaches the stand-in and the JSON read is
  // the thing under test.
  await context.grantPermissions(["local-network-access"], { origin: HOSTED_ORIGIN });
  const page = await context.newPage();
  await page.route(`${HOSTED_ORIGIN}/**`, (route) => fulfilPage(route, route.request().url()));
  await page.goto(`${HOSTED_PAGE}?port=${address.port}`, { waitUntil: "domcontentloaded" });
  await page.waitForFunction(
    () => document.querySelectorAll("#panels button").length === 7,
    undefined,
    { timeout: 15_000 },
  );
  return {
    page,
    port: address.port,
    close: async () => {
      await context.close();
      await new Promise<void>((closed) => stand.close(() => closed()));
    },
  };
}

// A failed status read states the connection message, the minimum release, the
// local-page fallback, and the conditional permission advice. It names no cause,
// because a page cannot tell a stopped server from an old one or a denied one.
const connectionMessage = (address: string): string =>
  `The page can't reach the harness server at ${address}. `
  + "The hosted page requires harness server v1.33 or later. "
  + "For an earlier release, open the local address that the server prints. "
  + "If the browser asks for permission to connect to your computer, allow it.";

describe("local page", () => {
  it("registers no worker and keeps every request on its own origin", async () => {
    const page = await browser.newPage();
    const requested: string[] = [];
    page.on("request", (request) => requested.push(request.url()));
    // A worker script that installs cleanly. A registration on the local page
    // then succeeds, so either signal proves that the page asked for no worker.
    await page.route(`${baseUrl}service-worker.js`, (route) => route.fulfill({
      status: 200,
      contentType: "text/javascript",
      body: "self.addEventListener('install', () => self.skipWaiting());\n",
    }));
    await page.goto(baseUrl, { waitUntil: "domcontentloaded" });
    await page.waitForFunction(
      () => document.querySelectorAll("#panels button").length === 7,
      undefined,
      { timeout: 15_000 },
    );
    await page.waitForTimeout(500);
    // A registration starts with a fetch of the worker script, so either
    // signal proves that the page asked for a worker.
    let registered = false;
    for (let step = 0; step < 30; step += 1) {
      const scopes = await page.evaluate(() =>
        navigator.serviceWorker.getRegistrations().then((list) => list.map((entry) => entry.scope)));
      if (scopes.length > 0 || requested.some((url) => url.endsWith("service-worker.js"))) {
        registered = true;
        break;
      }
      await page.waitForTimeout(100);
    }
    expect(registered).toBe(false);
    const offOrigin = requested.filter((url) => !url.startsWith(baseUrl));
    expect(offOrigin).toEqual([]);
    await page.context().close();
  }, 60_000);
});

describe("hosted page", () => {
  it("names every asset relative to the page", async () => {
    // Pages serves the page under /cmsc-127-harness/. A root-absolute asset
    // path asks for the account root and finds nothing.
    const page = await readFile(resolve(publicRoot, "index.html"), "utf-8");
    const references = [...page.matchAll(/(?:src|href)="([^"]+)"/g)].map((match) => match[1]);
    expect(references.filter((value) => value?.startsWith("/"))).toEqual([]);
  });

  it("waits for the Connect click before it polls", async () => {
    const { page, requested } = await hostedPage(STATUS_OK);
    const status = page.locator("#panel-content");
    expect(await status.textContent()).toContain(
      "Start the harness server on your computer, then click Connect.",
    );
    expect(await page.getByRole("button", { name: "Connect", exact: true }).count()).toBe(1);
    expect(requested).toEqual([]);
    await page.context().close();
  }, 60_000);

  it("points every panel except Status to the Status panel", async () => {
    // The Connect button is only in the Status panel. A prompt on another
    // panel that tells the reader to click it names a control that is not on
    // screen.
    const { page, requested } = await hostedPage(STATUS_OK);
    for (const name of ["Tables", "Changes", "Schema", "Sessions", "Checks", "Roles"]) {
      await page.getByRole("button", { name, exact: true }).click();
      const panel = page.locator("#panel-content");
      expect(await panel.textContent()).toContain(
        "Start the harness server on your computer, then open the Status panel and click Connect.",
      );
      expect(await page.getByRole("button", { name: "Connect", exact: true }).count()).toBe(0);
    }
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
      connectionMessage(SERVER),
    );
    await page.context().close();
  }, 60_000);

  it("gives the version and the local-page fallback when the browser withholds the status body", async () => {
    // This fixture repeats the real v1.32 server: status 200, no `api_version`
    // in the body, and no `Access-Control-Allow-Origin` header. The browser
    // withholds the body, so the page cannot tell that server from a stopped
    // one. It states the minimum release, the local route, and the permission
    // question, and it asserts no cause. The scratch repro also ran the real
    // v1.32 `server.py` against this page and got the same message.
    const stand = createServer((_request, response) => {
      response.writeHead(200, {
        "Content-Type": "application/json; charset=utf-8",
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
      });
      response.end(JSON.stringify({
        server: "running", databaseStatus: "connected",
        database: "campus", role: "ada", lastPoll: "2026-11-19T09:00:00Z",
      }));
    });
    await new Promise<void>((ready) => stand.listen(0, "127.0.0.1", ready));
    const address = stand.address();
    if (address === null || typeof address === "string") {
      throw new Error("The v1.32 stand-in did not bind to a port.");
    }
    const context = await browser.newContext();
    await context.grantPermissions(["local-network-access"], { origin: HOSTED_ORIGIN });
    const page = await context.newPage();
    try {
      await page.route(`${HOSTED_ORIGIN}/**`, (route) => fulfilPage(route, route.request().url()));
      await page.goto(`${HOSTED_PAGE}?port=${address.port}`, { waitUntil: "domcontentloaded" });
      await page.waitForFunction(
        () => document.querySelectorAll("#panels button").length === 7,
        undefined,
        { timeout: 15_000 },
      );
      await page.getByRole("button", { name: "Connect", exact: true }).click();
      await page.waitForFunction(
        () => {
          const message = document.querySelector("#connection-message");
          return message !== null && !message.hasAttribute("hidden")
            && (message.textContent ?? "").includes("v1.33");
        },
        undefined,
        { timeout: 15_000 },
      );
      const message = await page.locator("#connection-message").textContent();
      expect(message).toBe(connectionMessage(`http://127.0.0.1:${address.port}`));
      const panel = await page.locator("#panel-content").textContent();
      expect(panel).toContain(
        "Start the harness server on your computer, then click Connect.",
      );
    } finally {
      await context.close();
      await new Promise<void>((closed) => stand.close(() => closed()));
    }
  }, 60_000);

  it("reports a broken status body in one plain line", async () => {
    // A dev server or a notebook answers with HTML and `*`, so the browser
    // reads the body and the JSON read fails. The page must show the contract
    // line, never the JavaScript parse error.
    const { page, port, close } = await htmlServer();
    await page.getByRole("button", { name: "Connect", exact: true }).click();
    await page.waitForFunction(
      () => {
        const message = document.querySelector("#connection-message");
        return message !== null && !message.hasAttribute("hidden")
          && (message.textContent ?? "").length > 0;
      },
      undefined,
      { timeout: 15_000 },
    );
    const message = await page.locator("#connection-message").textContent();
    expect(message).toBe(connectionMessage(`http://127.0.0.1:${port}`));
    expect(message).not.toMatch(/Unexpected token|JSON|SyntaxError/i);
    await close();
  }, 60_000);

  it("reports a broken poll body in one plain line", async () => {
    // Connect succeeds on the status body, then a panel request answers with
    // HTML. The poll path reads JSON too, so it needs the same plain line.
    const stand = createServer((request, response) => {
      const isStatus = (request.url ?? "").startsWith("/api/status");
      response.writeHead(200, {
        "Content-Type": isStatus ? "application/json" : "text/html; charset=utf-8",
        "Access-Control-Allow-Origin": "*",
      });
      response.end(isStatus
        ? JSON.stringify({ server: "running", databaseStatus: "connected", api_version: 1 })
        : "<!doctype html><title>Another program</title><p>Not the harness.</p>");
    });
    await new Promise<void>((ready) => stand.listen(0, "127.0.0.1", ready));
    const address = stand.address();
    if (address === null || typeof address === "string") {
      throw new Error("The stand-in server did not bind to a port.");
    }
    const context = await browser.newContext();
    await context.grantPermissions(["local-network-access"], { origin: HOSTED_ORIGIN });
    const page = await context.newPage();
    try {
      await page.route(`${HOSTED_ORIGIN}/**`, (route) => fulfilPage(route, route.request().url()));
      await page.goto(`${HOSTED_PAGE}?port=${address.port}`, { waitUntil: "domcontentloaded" });
      await page.waitForFunction(
        () => document.querySelectorAll("#panels button").length === 7,
        undefined,
        { timeout: 15_000 },
      );
      await page.getByRole("button", { name: "Connect", exact: true }).click();
      await page.waitForFunction(
        () => {
          const message = document.querySelector("#connection-message");
          return message !== null && !message.hasAttribute("hidden")
            && (message.textContent ?? "").includes("can't reach the harness server");
        },
        undefined,
        { timeout: 15_000 },
      );
      const message = await page.locator("#connection-message").textContent();
      expect(message).toBe(connectionMessage(`http://127.0.0.1:${address.port}`));
      expect(message).not.toMatch(/Unexpected token|SyntaxError/i);
    } finally {
      await context.close();
      await new Promise<void>((closed) => stand.close(() => closed()));
    }
  }, 60_000);
});
