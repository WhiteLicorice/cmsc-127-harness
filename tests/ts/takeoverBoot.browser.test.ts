import { createServer, type Server } from "node:http";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { extname, join, resolve } from "node:path";
import { chromium, type Browser, type Page } from "playwright";
import { afterAll, beforeAll, describe, expect, it } from "vitest";

// A build can take over the tab while the previous document still boots. The
// document's later module request must then read its own build, not the new
// one. The first case starts a navigation during a successful activation and
// holds the first module request until the new worker is in charge.
//
// The second case holds the lock of the active snapshot during a cleanup. A
// second tab navigates in that window. Its record must still land, or the
// takeover answers the document's files from the new snapshot.

const root = resolve(import.meta.dirname, "../..");
const fixtureRoot = resolve(root, ".vitest/takeover-boot");

// The item lane owns ports 55533 to 55535. The test tries them in order and
// fails if all three are busy, so it never leaves the lane.
const LANE_PORTS = [55533, 55534, 55535];

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
  throw new Error(`The takeover test server found no free port in ${LANE_PORTS.join(", ")}.`);
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
export const version = "a";
`;
const APP_B = `if (document.getElementById("panel-b") === null) {
  throw new Error("The harness page is missing a required element.");
}
document.body.setAttribute("data-build", "b");
export const version = "b";
`;

type Build = { directory: string };

// The page loads app.js through its own script tag, so a later import of that
// path answers from the module map and never reaches the worker. probe.js is
// in the snapshot and in no script tag, so its import follows the worker.
async function writeBuild(name: string, html: string, app: string, version: string): Promise<Build> {
  const directory = resolve(fixtureRoot, name);
  await mkdir(directory, { recursive: true });
  await writeFile(join(directory, "index.html"), html, "utf-8");
  await writeFile(join(directory, "app.js"), app, "utf-8");
  await writeFile(join(directory, "probe.js"), `export const version = "${version}";\n`, "utf-8");
  const { writeOfflineFiles } = await import("../../scripts/build-offline.mjs");
  await writeOfflineFiles(directory);
  return { directory };
}

async function snapshotName(build: Build): Promise<string> {
  const manifest = await readFile(join(build.directory, "offline-manifest.json"), "utf-8");
  return `cmsc127-harness-${manifest.match(/"buildId":"([0-9a-f]{64})"/)?.[1] ?? ""}`;
}

// Hold the cleanup inside the lock of the active snapshot. The pause stands in
// for a slow check and open. A navigation in that window must still record its
// build.
async function holdInsideLock(build: Build, active: string): Promise<void> {
  const file = join(build.directory, "service-worker.js");
  const source = await readFile(file, "utf-8");
  const marker = "cache = await openSnapshot(name);";
  const target = source.indexOf(marker, source.indexOf("async function deleteUnusedSnapshots()"));
  if (target < 0) throw new Error("The worker template has no cleanup open to hold.");
  const held = `${source.slice(0, target)}if (name === ${JSON.stringify(active)}) await fetch(scoped("__held_lock__?n=" + holdCount++));\n${source.slice(target)}`;
  await writeFile(file, `let holdCount = 0;\n${held}`, "utf-8");
}

// A reload starts a cleanup through the navigation handler. This listener
// starts one on demand, so the test can hold the lock without a reload.
async function addCleanupMessage(build: Build): Promise<void> {
  const file = join(build.directory, "service-worker.js");
  const source = await readFile(file, "utf-8");
  await writeFile(file, `${source}
self.addEventListener("message", (event) => {
  if (event.data !== "cleanup") return;
  event.waitUntil(deleteUnusedSnapshots().then(() => event.ports[0].postMessage("done")));
});
`, "utf-8");
}

let buildA: Build;
let buildB: Build;
let server: Server;
let browser: Browser;
let origin = "";
let served: Build;

let holdLock = false;
let announceHold: () => void = () => undefined;
let releaseHold: () => void = () => undefined;
const lockHeld = new Promise<void>((resolve) => {
  announceHold = resolve;
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
  if (relative === "__held_lock__") {
    if (holdLock) {
      holdLock = false;
      const gate = new Promise<void>((release) => {
        releaseHold = release;
      });
      announceHold();
      await gate;
    }
    response.writeHead(204, { "Cache-Control": "no-store" });
    response.end();
    return;
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
  buildA = await writeBuild("build-a", PAGE_A, APP_A, "a");
  await holdInsideLock(buildA, await snapshotName(buildA));
  await addCleanupMessage(buildA);
  buildB = await writeBuild("build-b", PAGE_B, APP_B, "b");
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

// Read the module through a real module script. A test-runner dynamic import
// would be rewritten by the test bundler.
type Imported = Record<string, unknown>;

async function importVersion(page: Page): Promise<Imported> {
  return page.evaluate<Imported>(() => new Promise((resolve) => {
    type WindowWithImport = Window & { imported?: Imported };
    const target = window as WindowWithImport;
    target.imported = undefined;
    const script = document.createElement("script");
    script.type = "module";
    script.textContent = `import("./probe.js").then(
      (module) => { window.imported = { version: module.version }; },
      (error) => { window.imported = { error: error.message }; },
    );`;
    document.body.append(script);
    const wait = (): void => {
      if (target.imported !== undefined) {
        resolve(target.imported);
        return;
      }
      setTimeout(wait, 50);
    };
    wait();
  }));
}

describe("takeover while a document boots", () => {
  it("keeps an open document on the build that served it", async () => {
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

    // The server offers build B. The update installs and the new worker claims
    // the open page, so the next module request meets the new worker.
    served = buildB;
    await page.evaluate(() => {
      window.before = navigator.serviceWorker.controller;
    });
    await page.evaluate(async () => {
      const registration = await navigator.serviceWorker.getRegistration();
      await registration?.update();
    });
    const cacheB = await snapshotName(buildB);
    await waitForCache(page, cacheB);
    await page.waitForFunction(
      () => navigator.serviceWorker.controller !== window.before,
      undefined,
      { timeout: 15_000 },
    );

    // The open page still holds build A's document, so its first module
    // request must read build A's app.
    errors.length = 0;
    const first = await importVersion(page);
    expect(errors).toEqual([]);
    expect(first["version"]).toBe("a");
    expect(await page.evaluate(() => document.getElementById("panel-a") !== null)).toBe(true);

    // The next navigation meets build B's document, the active page, and the
    // module request follows the new build.
    await page.reload({ waitUntil: "load" });
    expect(await page.evaluate(() => document.body.dataset["build"])).toBe("b");
    expect((await importVersion(page))["version"]).toBe("b");

    await context.close();
  }, 120_000);

  it("records the build of a document that navigates during a cleanup lock", async () => {
    const context = await browser.newContext();
    const first = await context.newPage();
    const errors: string[] = [];
    first.on("pageerror", (error) => errors.push(error.message));

    served = buildA;
    await first.goto(`${origin}/`, { waitUntil: "load" });
    await first.evaluate(async () => {
      await navigator.serviceWorker.register("service-worker.js", { scope: "./" });
      await navigator.serviceWorker.ready;
    });
    const cacheA = await snapshotName(buildA);
    await waitForCache(first, cacheA);
    await first.reload({ waitUntil: "load" });
    expect(await first.evaluate(() => navigator.serviceWorker.controller !== null)).toBe(true);

    // The cleanup takes the lock of the active snapshot and holds it.
    holdLock = true;
    const cleanup = first.evaluate(() => new Promise<void>((resolve) => {
      const channel = new MessageChannel();
      channel.port1.onmessage = () => resolve();
      navigator.serviceWorker.controller?.postMessage("cleanup", [channel.port2]);
    }));
    await lockHeld;

    // A second tab navigates in that window. The navigation must wait for the
    // lock. Its record must land.
    const second = await context.newPage();
    const started = Date.now();
    const navigation = second.goto(`${origin}/`, { waitUntil: "load" });
    const settled = await Promise.race([
      navigation.then(() => "done" as const),
      new Promise<"pending">((resolve) => setTimeout(() => resolve("pending"), 500)),
    ]);
    releaseHold();
    await navigation;
    const waited = Date.now() - started;
    await cleanup;

    // The record holds build A, so the takeover keeps this document on A.
    served = buildB;
    await second.evaluate(() => {
      window.before = navigator.serviceWorker.controller;
    });
    await second.evaluate(async () => {
      const registration = await navigator.serviceWorker.getRegistration();
      await registration?.update();
    });
    const cacheB = await snapshotName(buildB);
    await waitForCache(second, cacheB);
    await second.waitForFunction(
      () => navigator.serviceWorker.controller !== window.before,
      undefined,
      { timeout: 15_000 },
    );

    errors.length = 0;
    const imported = await importVersion(second);
    expect(errors).toEqual([]);
    expect(imported["version"]).toBe("a");

    // The navigation met the lock, so it waited for it. A navigation that
    // finished inside the hold never met the lock. This case then proves
    // nothing.
    expect(settled).toBe("pending");
    expect(waited).toBeGreaterThanOrEqual(400);

    await context.close();
  }, 120_000);
});
