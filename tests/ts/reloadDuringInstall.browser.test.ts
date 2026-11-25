import { createServer, type Server } from "node:http";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { extname, join, resolve } from "node:path";
import { chromium, type Browser, type Page } from "playwright";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { listenOnFreePort } from "./listen";

// A student reloads while a second build downloads. The active worker runs its
// cleanup during that download. That cleanup must keep the snapshot which the
// install still fills, or the next offline navigation has no page. This test
// holds one file of build B, reloads, releases the file, then goes offline.

const root = resolve(import.meta.dirname, "../..");
const fixtureRoot = resolve(root, ".vitest/reload-during-install");

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
// holds the first page file until it has captured the new worker. That is the
// download window in which the active worker runs its cleanup.
let holdApp = false;
// The server answers the page file with 503, so build B cannot install. The
// page then stays on build A, which is the case the failed-install test states.
let failApp = false;
// The number of page-file downloads the fixture refused. The failed-install
// test waits for the first one, so a run that never reached build B's download
// cannot pass for a failed install.
let refused = 0;
const refusedWaiters: (() => void)[] = [];
let releaseApp: () => void = () => undefined;
// Each held page-file download announces itself, so the test can act while the
// download is open. The queue lets both cases hold one.
const holdWaiters: (() => void)[] = [];

function nextHold(): Promise<void> {
  return new Promise<void>((ready) => holdWaiters.push(ready));
}

// Wait for the fixture to refuse one page-file download. A run that serves
// build A in place of build B never refuses one, so this wait reports it.
async function waitForRefusal(seconds: number): Promise<void> {
  if (refused > 0) return;
  await Promise.race([
    new Promise<void>((resolve) => refusedWaiters.push(resolve)),
    new Promise<void>((_, reject) => {
      setTimeout(
        () => reject(new Error("The fixture never refused build B's download.")),
        seconds * 1000,
      );
    }),
  ]);
}

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
  // The hold comes first, so the test can capture the new worker while the
  // download is open and only then decide how to answer it.
  if (holdApp && relative === "app.js") {
    holdApp = false;
    const gate = new Promise<void>((release) => {
      releaseApp = release;
    });
    for (const wake of holdWaiters.splice(0)) wake();
    await gate;
  }
  if (failApp && relative === "app.js") {
    refused += 1;
    for (const wake of refusedWaiters.splice(0)) wake();
    response.writeHead(503);
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
  buildA = await writeBuild("build-a", PAGE_A, APP_A);
  buildB = await writeBuild("build-b", PAGE_B, APP_B);
  served = buildA;
  server = createServer((request, response) => {
    void handle(request, response);
  });
  const port = await listenOnFreePort(server, "reload-during-install");
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

type ControllerState = {
  installing: string | null;
  waiting: string | null;
  active: string | null;
  controller: string | null;
  same: boolean;
  changed: boolean;
};

// The controller that build A installed. The wait compares against it, so a
// registration that still reports A cannot pass for the new build.
async function captureController(page: Page): Promise<void> {
  await page.evaluate(() => {
    Object.assign(window, { harnessControllerA: navigator.serviceWorker.controller });
  });
}

// Attach the updatefound listener before the update, so the test holds the
// worker that the browser would otherwise retire between two polls. The worker
// and the update promise stay on the window, so the next call can wait for the
// captured worker's state.
async function startUpdateAndCapture(page: Page): Promise<string> {
  return page.evaluate(async () => {
    const registration = await navigator.serviceWorker.getRegistration();
    if (registration === null) return "no registration";
    const found = new Promise<ServiceWorker | null>((ready) => {
      registration.addEventListener(
        "updatefound",
        () => ready(registration.installing),
        { once: true },
      );
    });
    const updating = registration.update();
    Object.assign(window, { harnessUpdating: updating });
    const worker = await Promise.race([
      found,
      new Promise<null>((ready) => setTimeout(() => ready(null), 20_000)),
    ]);
    if (worker === null) return "no installing worker";
    Object.assign(window, { harnessWorkerB: worker });
    return worker.state;
  });
}

// Wait for the captured worker to become redundant. The wait reads the worker
// the listener caught, so a browser that retires it between two polls cannot
// hide it from this wait.
async function waitForRedundant(page: Page): Promise<string> {
  return page.evaluate(async () => {
    const worker = Reflect.get(window, "harnessWorkerB") as ServiceWorker | undefined;
    if (worker === undefined) return "no captured worker";
    const deadline = Date.now() + 20_000;
    while (worker.state !== "redundant" && Date.now() < deadline) {
      await new Promise((ready) => setTimeout(ready, 10));
    }
    const updating = Reflect.get(window, "harnessUpdating") as Promise<unknown> | undefined;
    await updating?.catch(() => undefined);
    return worker.state;
  });
}

// The install finishes when no install or wait sits in the registration. That
// is not enough. Chromium can report both as null while the controller is
// still activating. The offline navigation then reads the old build. Wait for
// a controller that differs from the one build A installed, and for that
// controller to have finished activating. The states seen on the way are
// kept, so a later failure shows the order.
async function waitForActivatedController(page: Page): Promise<void> {
  const seen: string[] = [];
  let changed = false;
  for (let step = 0; step < 200; step += 1) {
    const state = await page.evaluate(async (): Promise<ControllerState> => {
      const registration = await navigator.serviceWorker.getRegistration();
      const controller = navigator.serviceWorker.controller;
      const captured = Reflect.get(window, "harnessControllerA") as
        | ServiceWorker
        | null
        | undefined;
      return {
        installing: registration?.installing?.state ?? null,
        waiting: registration?.waiting?.state ?? null,
        active: registration?.active?.state ?? null,
        controller: controller?.state ?? null,
        same: controller !== null && controller === registration?.active,
        changed: captured !== undefined && controller !== captured,
      };
    });
    seen.push(JSON.stringify(state));
    if (state.changed) changed = true;
    if (
      state.installing === null
      && state.waiting === null
      && state.active === "activated"
      && state.controller === "activated"
      && state.same
      && state.changed
    ) {
      return;
    }
    await page.waitForTimeout(50);
  }
  if (!changed) {
    throw new Error(`The old controller stayed in control. States: ${seen.join(" ")}`);
  }
  throw new Error(`The new controller never finished activating. States: ${seen.join(" ")}`);
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
    const held = nextHold();
    await page.evaluate(() => {
      void navigator.serviceWorker.getRegistration().then((registration) => registration?.update());
    });
    await held;

    // The reload runs the build A cleanup while build B still downloads. The
    // page must still come from build A, which answers it.
    await page.reload({ waitUntil: "load" });
    expect(await page.evaluate(() => document.body.dataset["build"])).toBe("a");
    // Keep the controller that build A installed. The wait below must see a
    // different one before the offline navigation can read build B.
    await captureController(page);

    releaseApp();
    const cacheB = await snapshotName(buildB);
    // The install finishes, then its activation ends. The offline navigation
    // must read the new build. Wait until the new controller has finished
    // activating. An idle-looking registration can pass before that point.
    await waitForActivatedController(page);
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

  it("reports that the old controller stayed in control when the install fails", async () => {
    const context = await browser.newContext();
    const page = await context.newPage();
    served = buildA;
    failApp = false;
    refused = 0;
    await page.goto(`${origin}/`, { waitUntil: "load" });
    await page.evaluate(async () => {
      await navigator.serviceWorker.register("service-worker.js", { scope: "./" });
      await navigator.serviceWorker.ready;
    });
    await waitForCache(page, await snapshotName(buildA));
    await page.reload({ waitUntil: "load" });
    await captureController(page);

    // Build A controls the page before the update. The case states that A
    // keeps the page, so the test must first see A in control.
    expect(await page.evaluate(() => document.body.dataset["build"])).toBe("a");
    expect(await page.evaluate(() => navigator.serviceWorker.controller !== null)).toBe(true);

    // The server offers build B and holds its page file, so the test captures
    // build B's worker before the browser can retire it. The 503 goes out only
    // after the capture, so the install cannot finish first.
    served = buildB;
    failApp = false;
    holdApp = true;
    const held = nextHold();
    const captured = await startUpdateAndCapture(page);
    expect(captured).not.toBe("no installing worker");
    await held;
    failApp = true;
    releaseApp();
    expect(await waitForRedundant(page)).toBe("redundant");
    await waitForRefusal(20);
    expect(refused).toBeGreaterThan(0);

    // The wait must report that A stayed in control. A later snapshot check
    // would report the wrong fault. The fixture keeps refusing the page file,
    // so a retry cannot install build B and pass for the failed install.
    await expect(waitForActivatedController(page)).rejects.toThrow("stayed in control");
    failApp = false;

    // Build A still controls the page. Its snapshot is still there.
    expect(await page.evaluate(() => document.body.dataset["build"])).toBe("a");
    expect(await page.evaluate(() => navigator.serviceWorker.controller !== null)).toBe(true);
    expect(await caches(page)).toContain(await snapshotName(buildA));

    await context.close();
  }, 120_000);
});
