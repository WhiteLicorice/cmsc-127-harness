import { createServer, type Server } from "node:http";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { extname, join, resolve } from "node:path";
import { chromium, type Browser, type Page } from "playwright";
import { afterAll, beforeAll, describe, expect, it } from "vitest";

// A student reloads while a second build downloads. The active worker runs its
// cleanup during that download. That cleanup must keep the snapshot which the
// install still fills, or the next offline navigation has no page. This test
// holds one file of build B, reloads, releases the file, then goes offline.

const root = resolve(import.meta.dirname, "../..");
const fixtureRoot = resolve(root, ".vitest/reload-during-install");

// The release lane owns ports 55536 to 55538. The item gate moves them into its
// own range. The test fails when the lane port is busy, so it stays in the lane.
const LANE_PORTS = [55536, 55537, 55538];

async function listenOnLanePort(target: Server): Promise<number> {
  for (const port of LANE_PORTS) {
    const taken = await new Promise<boolean>((ready) => {
      const failed = (error: NodeJS.ErrnoException) => {
        target.off("listening", listened);
        ready(error.code === "EADDRINUSE");
      };
      const listened = () => {
        target.off("error", failed);
        ready(false);
      };
      target.once("error", failed);
      target.once("listening", listened);
      target.listen(port, "127.0.0.1");
    });
    if (!taken) return port;
  }
  throw new Error(`The reload-during-install test server found no free port in ${LANE_PORTS.join(", ")}.`);
}

const TYPES: Record<string, string> = {
  ".html": "text/html", ".js": "text/javascript", ".json": "application/json",
};

const PAGE_A = `<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Build A</title></head>
<body><div id="panel-a"></div><script type="module" src="app.js"></script></body>
</html>
`;
const PAGE_B = `<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Build B</title></head>
<body><div id="panel-b"></div><script type="module" src="app.js"></script></body>
</html>
`;
const APP_A = `if (document.getElementById("panel-a") === null) {
  throw new Error("The harness page is missing a required element.");
}
document.body.setAttribute("data-build", "a");
`;
const APP_B = `if (document.getElementById("panel-b") === null) {
  throw new Error("The harness page is missing a required element.");
}
document.body.setAttribute("data-build", "b");
`;

type Build = { directory: string };

async function writeBuild(name: string, html: string, app: string): Promise<Build> {
  const directory = resolve(fixtureRoot, name);
  await mkdir(directory, { recursive: true });
  await writeFile(join(directory, "index.html"), html, "utf-8");
  await writeFile(join(directory, "app.js"), app, "utf-8");
  const { writeOfflineFiles } = await import("../../scripts/build-offline.mjs");
  await writeOfflineFiles(directory);
  return { directory };
}

async function snapshotName(build: Build): Promise<string> {
  const manifest = await readFile(join(build.directory, "offline-manifest.json"), "utf-8");
  return `cmsc127-harness-${manifest.match(/"buildId":"([0-9a-f]{64})"/)?.[1] ?? ""}`;
}

let buildA: Build;
let buildB: Build;
let server: Server;
let browser: Browser;
let origin = "";
let served: Build;

// The install asks for the manifest first, then for each page file. The test
// holds the first page file until the reload has run. That is the download
// window in which the active worker runs its cleanup.
let holdApp = false;
let releaseApp: () => void = () => undefined;
let announceHeld: () => void = () => undefined;
const held = new Promise<void>((resolve) => {
  announceHeld = resolve;
});

function serveFile(response: import("node:http").ServerResponse, body: Buffer, type: string): void {
  response.writeHead(200, { "Content-Type": type, "Cache-Control": "no-store" });
  response.end(body);
}

async function handle(
  request: import("node:http").IncomingMessage,
  response: import("node:http").ServerResponse,
): Promise<void> {
  const pathname = new URL(request.url ?? "/", "http://127.0.0.1").pathname;
  const relative = pathname === "/" ? "index.html" : pathname.slice(1);
  if (holdApp && relative === "app.js") {
    holdApp = false;
    const gate = new Promise<void>((release) => {
      releaseApp = release;
    });
    announceHeld();
    await gate;
  }
  const file = join(served.directory, relative);
  const body = await readFile(file).catch(() => null);
  if (body === null) {
    response.writeHead(404);
    response.end();
    return;
  }
  serveFile(response, body, TYPES[extname(file)] ?? "application/octet-stream");
}

beforeAll(async () => {
  buildA = await writeBuild("build-a", PAGE_A, APP_A);
  buildB = await writeBuild("build-b", PAGE_B, APP_B);
  served = buildA;
  server = createServer((request, response) => {
    void handle(request, response);
  });
  const port = await listenOnLanePort(server);
  origin = `http://127.0.0.1:${port}`;
  browser = await chromium.launch({ headless: true });
}, 60_000);

afterAll(async () => {
  await browser?.close();
  await new Promise<void>((closed) => server?.close(() => closed()));
});

async function caches(page: Page): Promise<string[]> {
  return page.evaluate(() => caches.keys());
}

async function waitForCache(page: Page, name: string): Promise<void> {
  for (let step = 0; step < 150; step += 1) {
    if ((await caches(page)).includes(name)) return;
    await page.waitForTimeout(100);
  }
  throw new Error(`The snapshot ${name} never appeared.`);
}

describe("a reload while a new build downloads", () => {
  it("keeps the new snapshot and serves it offline", async () => {
    const context = await browser.newContext();
    const page = await context.newPage();
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));

    await page.goto(`${origin}/`, { waitUntil: "load" });
    await page.evaluate(async () => {
      await navigator.serviceWorker.register("service-worker.js", { scope: "./" });
      await navigator.serviceWorker.ready;
    });
    const cacheA = await snapshotName(buildA);
    await waitForCache(page, cacheA);

    // The first load is not controlled. One reload puts the worker in charge.
    await page.reload({ waitUntil: "load" });
    expect(await page.evaluate(() => navigator.serviceWorker.controller !== null)).toBe(true);
    expect(await page.evaluate(() => document.body.dataset["build"])).toBe("a");

    // The server offers build B, with the first page file held open. The
    // install writes its cache record and then waits inside the download.
    served = buildB;
    holdApp = true;
    await page.evaluate(() => {
      void navigator.serviceWorker.getRegistration().then((registration) => registration?.update());
    });
    await held;

    // The reload runs the build A cleanup while build B still downloads. The
    // page must still come from build A, which answers it.
    await page.reload({ waitUntil: "load" });
    expect(await page.evaluate(() => document.body.dataset["build"])).toBe("a");

    releaseApp();
    const cacheB = await snapshotName(buildB);
    // The install finishes. Its activation ends when no install or wait sits
    // in the registration, whether or not its snapshot survived.
    await page.waitForFunction(
      async () => {
        const registration = await navigator.serviceWorker.getRegistration();
        return registration?.waiting === null && registration?.installing === null;
      },
      undefined,
      { timeout: 20_000 },
    );
    const kept = await caches(page);

    // Offline. The next navigation must read the snapshot that build B filled.
    await context.setOffline(true);
    const answer = await page.reload({ waitUntil: "load" }).catch(() => null);
    const status = answer?.status() ?? 0;
    // State the defect in the failure message: the reload removed the snapshot.
    expect({ status, kept: kept.includes(cacheB) }).toEqual({ status: 200, kept: true });
    expect(errors).toEqual([]);
    expect(await page.evaluate(() => document.body.dataset["build"])).toBe("b");
    // The cleanup of the retired build may still run. This test states that the
    // new snapshot is still there, not the order of the older one's removal.
    expect(await caches(page)).toContain(cacheB);

    await context.close();
  }, 120_000);
});
