import { createServer, type Server } from "node:http";
import { readFile } from "node:fs/promises";
import { extname, resolve } from "node:path";
import { webkit, type Browser, type Page } from "playwright";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { closeServer } from "./listen";

// Safari has no hosted-page connection to a local port. The manual names the
// local page as its route. This test proves both halves of that route in
// WebKit: the hosted page states the local-page fallback, and the local page
// loads its seven panels.
const root = resolve(import.meta.dirname, "../..");
// CMSC127_PAGE_ROOT serves a scratch copy of the page. The red run uses it to
// remove the fallback sentence and prove that this test catches the loss.
const publicRoot = resolve(process.env["CMSC127_PAGE_ROOT"] ?? resolve(root, "public"));
const HOSTED_ORIGIN = "https://whitelicorice.github.io";
const HOSTED_PAGE = `${HOSTED_ORIGIN}/cmsc-127-harness/`;
const LOCAL_PAGE_ADVICE = "open the local address that the server prints";

const TYPES: Record<string, string> = {
  ".html": "text/html", ".css": "text/css", ".js": "text/javascript", ".txt": "text/plain",
};

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

let server: Server;
let browser: Browser;
let baseUrl = "";
// A port with nothing behind it. WebKit refuses the loopback request, so the
// page takes the same route a Safari reader takes.
let closedPort = 0;

beforeAll(async () => {
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
    throw new Error("The WebKit fallback test server did not bind to a port.");
  }
  baseUrl = `http://127.0.0.1:${address.port}/`;
  const probe = createServer();
  await new Promise<void>((ready) => probe.listen(0, "127.0.0.1", ready));
  const probeAddress = probe.address();
  if (probeAddress === null || typeof probeAddress === "string") {
    throw new Error("The closed-port probe did not bind to a port.");
  }
  closedPort = probeAddress.port;
  await new Promise<void>((closed) => probe.close(() => closed()));
  browser = await webkit.launch({ headless: true });
}, 60_000);

afterAll(async () => {
  await browser?.close();
  await closeServer(server);
});

describe("WebKit local-page fallback", () => {
  it("states the local-page fallback when Safari cannot reach the server", async () => {
    const context = await browser.newContext();
    const page: Page = await context.newPage();
    try {
      await page.route(`${HOSTED_ORIGIN}/**`, (route) => pageFile(new URL(route.request().url()).pathname)
        .then((file) => (file === null
          ? route.fulfill({ status: 404, body: "missing" })
          : route.fulfill({ status: 200, contentType: file.type, body: file.body }))));
      await page.goto(`${HOSTED_PAGE}?port=${closedPort}`, { waitUntil: "domcontentloaded" });
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
            && (message.textContent ?? "").length > 0;
        },
        undefined,
        { timeout: 15_000 },
      );
      const message = await page.locator("#connection-message").textContent();
      expect(message).toContain(LOCAL_PAGE_ADVICE);
      expect(message).toContain(`http://127.0.0.1:${closedPort}`);
      expect(message).not.toContain("too old");
    } finally {
      await context.close();
    }
  }, 60_000);

  it("loads the local page that the fallback names", async () => {
    const page = await browser.newPage();
    try {
      await page.goto(baseUrl, { waitUntil: "domcontentloaded" });
      await page.waitForFunction(
        () => document.querySelectorAll("#panels button").length === 7,
        undefined,
        { timeout: 15_000 },
      );
      // The local page sends its requests to its own origin, so it needs no
      // Connect button and no permission prompt.
      expect(await page.getByRole("button", { name: "Connect", exact: true }).count()).toBe(0);
      expect(await page.locator("#panels button").allTextContents()).toEqual(
        ["Tables", "Changes", "Schema", "Sessions", "Checks", "Roles", "Status"],
      );
    } finally {
      await page.context().close();
    }
  }, 60_000);
});
