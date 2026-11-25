import { createServer, type Server } from "node:http";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { extname, join, resolve } from "node:path";
import { chromium, type Browser, type Page } from "playwright";
import { afterAll, beforeAll, describe, expect, it } from "vitest";

// A student reloads while a second build downloads. The install of the second
// build can open its snapshot after the active worker has read the cache list.
// A cleanup that reads the list a second time for its deletes meets that new
// snapshot with no classification, and removes it. The next offline navigation
// then has no page.
//
// The test pauses the active worker one step after its list. The pause stands
// in for a slow list. The update starts in that window, then the cleanup
// finishes, and the page goes offline.

const root = resolve(import.meta.dirname, "../..");
const fixtureRoot = resolve(root, ".vitest/reload-during-open-install");

// The release lane owns ports 55539 to 55541, which no other browser test
// uses. The item gate moves them into its own range. The test fails when the
// lane port is busy, so it stays in the lane.
//
// A port is free only when this process binds it. Any bind error counts as
// taken. A blocked port (EACCES) would otherwise read as free, and the test
// would fail later with a message about a refused connection.
const LANE_PORTS = [55539, 55540, 55541];

async function listenOnLanePort(target: Server): Promise<number> {
  const failures: string[] = [];
  for (const port of LANE_PORTS) {
    const failure = await new Promise<NodeJS.ErrnoException | null>((ready) => {
      const failed = (error: NodeJS.ErrnoException) => {
        target.off("listening", listened);
        ready(error);
      };
      const listened = () => {
        target.off("error", failed);
        ready(null);
      };
      target.once("error", failed);
      target.once("listening", listened);
      target.listen(port, "127.0.0.1");
    });
    if (failure === null) return port;
    failures.push(`${port}: ${failure.code ?? failure.message}`);
  }
  throw new Error(
    `The reload-during-open-install test server found no free port in ${LANE_PORTS.join(", ")}. ` +
      `Bind errors: ${failures.join(", ")}.`,
  );
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

// Pause the first worker one step after its cache list, and before it acts on
// the names. Every CacheStorage call still runs in Chromium. The server decides
// when that step can continue.
async function holdAfterList(build: Build): Promise<void> {
  const file = join(build.directory, "service-worker.js");
  const source = await readFile(file, "utf-8");
  const loop = "for (const name of await caches.keys()) {";
  const target = source.indexOf(loop, source.indexOf("async function deleteUnusedSnapshots()"));
  if (target < 0) throw new Error("The worker template has no cleanup list to hold.");
  await writeFile(
    file,
    `${source.slice(0, target)}const listed = await caches.keys();
    await fetch(scoped("__held_scan__"));
    for (const name of listed) {${source.slice(target + loop.length)}`,
    "utf-8",
  );
}

let buildA: Build;
let buildB: Build;
let server: Server;
let browser: Browser;
let origin = "";
let served: Build;

let holdScan = false;
let announceScan: () => void = () => undefined;
let releaseScan: () => void = () => undefined;
const scanHeld = new Promise<void>((resolve) => {
  announceScan = resolve;
});

let holdApp = false;
let announceApp: () => void = () => undefined;
let releaseApp: () => void = () => undefined;
const appHeld = new Promise<void>((resolve) => {
  announceApp = resolve;
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
  if (relative === "__held_scan__") {
    if (holdScan) {
      holdScan = false;
      const gate = new Promise<void>((release) => {
        releaseScan = release;
      });
      announceScan();
      await gate;
    }
    response.writeHead(204, { "Cache-Control": "no-store" });
    response.end();
    return;
  }
  if (holdApp && relative === "app.js" && served === buildB) {
    holdApp = false;
    const gate = new Promise<void>((release) => {
      releaseApp = release;
    });
    announceApp();
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
  await holdAfterList(buildA);
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

describe("a reload during an install that opens its snapshot", () => {
  it("keeps the snapshot that the install opened after the cache list", async () => {
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
    await page.reload({ waitUntil: "load" });
    expect(await page.evaluate(() => navigator.serviceWorker.controller !== null)).toBe(true);

    // The reload runs the active worker's cleanup. It reads its list and then
    // holds, before it acts on a name. The reload itself finishes, because the
    // cleanup runs in the wait of its own event.
    holdScan = true;
    await page.reload({ waitUntil: "load" });
    let held = false;
    for (let step = 0; step < 100 && !held; step += 1) {
      held = await Promise.race([
        scanHeld.then(() => true),
        page.waitForTimeout(100).then(() => false),
      ]);
    }
    expect(held).toBe(true);

    // The server now offers build B. The install opens its snapshot in this
    // window, so the cleanup's list does not name it.
    served = buildB;
    holdApp = true;
    await page.evaluate(() => {
      void navigator.serviceWorker.getRegistration().then((registration) => registration?.update());
    });
    let downloading = false;
    for (let step = 0; step < 100 && !downloading; step += 1) {
      downloading = await Promise.race([
        appHeld.then(() => true),
        page.waitForTimeout(100).then(() => false),
      ]);
    }
    expect(downloading).toBe(true);
    const cacheB = await snapshotName(buildB);
    await waitForCache(page, cacheB);

    // The cleanup now acts on the list it took before the install opened.
    releaseScan();
    releaseApp();
    // The install finishes. Its activation ends when no install or wait sits in
    // the registration, whether or not its snapshot survived.
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
    // State the defect in the failure message: the cleanup removed the snapshot.
    expect({ status, kept: kept.includes(cacheB) }).toEqual({ status: 200, kept: true });
    expect(errors).toEqual([]);

    await context.close();
  }, 120_000);
});
