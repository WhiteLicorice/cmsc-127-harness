import { createServer, type Server } from "node:http";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { extname, join, resolve } from "node:path";
import { chromium, type Browser, type Page } from "playwright";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { listenOnFreePort } from "./listen";

// Two cleanups can overlap. A reload starts one. A second reload starts another
// before the first ends. The first reads the cache list. A second pass deletes
// an expired snapshot under its lock. The first then opens the missing cache.
// An open outside the lock plants an empty cache. A later pass keeps an empty
// cache because an install opens its snapshot before it writes its record.
//
// The test pauses the first cleanup one step after its list. That pause stands
// in for a slow list. A second cleanup runs to its end in the window. The first
// then finishes. A third pass shows what the first left behind.

const root = resolve(import.meta.dirname, "../..");
const fixtureRoot = resolve(root, ".vitest/concurrent-cleanup");

const TYPES: Record<string, string> = {
  ".html": "text/html", ".js": "text/javascript", ".json": "application/json",
};

const PAGE = `<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Concurrent cleanup</title></head>
<body><div id="panel-a"></div><script type="module" src="app.js"></script></body>
</html>
`;
const APP = `if (document.getElementById("panel-a") === null) {
  throw new Error("The harness page is missing a required element.");
}
document.body.setAttribute("data-build", "a");
`;

type Build = { directory: string };

async function writeBuild(name: string): Promise<Build> {
  const directory = resolve(fixtureRoot, name);
  await mkdir(directory, { recursive: true });
  await writeFile(join(directory, "index.html"), PAGE, "utf-8");
  await writeFile(join(directory, "app.js"), APP, "utf-8");
  const { writeOfflineFiles } = await import("../../scripts/build-offline.mjs");
  await writeOfflineFiles(directory);
  return { directory };
}

async function snapshotName(build: Build): Promise<string> {
  const manifest = await readFile(join(build.directory, "offline-manifest.json"), "utf-8");
  return `cmsc127-harness-${manifest.match(/"buildId":"([0-9a-f]{64})"/)?.[1] ?? ""}`;
}

// Pause the cleanup one step after its cache list, and before it acts on the
// list. Every CacheStorage call still runs in Chromium. The test server
// releases that step. The request carries a counter, because Chromium holds a
// second request to the same URL behind the first.
//
// The last line of the cleanup reports that the pass ended, so the test can
// wait for the activation pass before it starts its own.
async function holdAfterList(build: Build): Promise<void> {
  const file = join(build.directory, "service-worker.js");
  const source = await readFile(file, "utf-8");
  const loop = "for (const name of await caches.keys()) {";
  const target = source.indexOf(loop, source.indexOf("async function deleteUnusedSnapshots()"));
  if (target < 0) throw new Error("The worker template has no cleanup list to hold.");
  const tail = "  await Promise.all(unused.map((name) => deleteSnapshot(name)));\n}";
  const end = source.indexOf(tail, target);
  if (end < 0) throw new Error("The worker template has no cleanup end to report.");
  const held = `${source.slice(0, target)}const listed = await caches.keys();
    await fetch(scoped("__held_scan__?n=" + holdCount++));
    for (const name of listed) {${source.slice(target + loop.length, end)}${tail.slice(0, -1)}
  await fetch(scoped("__scan_done__"));
}${source.slice(end + tail.length)}`;
  await writeFile(file, `let holdCount = 0;\n${held}`, "utf-8");
}

// A reload starts a cleanup through the navigation handler. This listener
// starts one on demand, so the test can overlap two of them.
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

let build: Build;
let server: Server;
let browser: Browser;
let origin = "";

let holdScan = false;
let scansDone = 0;
let announceScan: () => void = () => undefined;
let releaseScan: () => void = () => undefined;
const scanHeld = new Promise<void>((resolve) => {
  announceScan = resolve;
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
  if (relative === "__scan_done__") {
    scansDone += 1;
    response.writeHead(204, { "Cache-Control": "no-store" });
    response.end();
    return;
  }
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
  const file = join(build.directory, relative);
  const body = await readFile(file).catch(() => null);
  if (body === null) {
    response.writeHead(404);
    response.end();
    return;
  }
  serveFile(response, body, TYPES[extname(file)] ?? "application/octet-stream");
}

beforeAll(async () => {
  build = await writeBuild("build-a");
  await holdAfterList(build);
  await addCleanupMessage(build);
  server = createServer((request, response) => {
    void handle(request, response);
  });
  const port = await listenOnFreePort(server, "concurrent-cleanup");
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

async function waitForScans(page: Page, count: number): Promise<void> {
  for (let step = 0; step < 150; step += 1) {
    if (scansDone >= count) return;
    await page.waitForTimeout(100);
  }
  throw new Error(`The cleanup ended ${scansDone} times, not ${count}.`);
}

// Run one cleanup in the worker and wait for its answer.
function cleanup(page: Page): Promise<void> {
  return page.evaluate(() => new Promise<void>((resolve) => {
    const channel = new MessageChannel();
    channel.port1.onmessage = () => resolve();
    navigator.serviceWorker.controller?.postMessage("cleanup", [channel.port2]);
  }));
}

describe("two cleanups that overlap", () => {
  it("does not plant a snapshot that the other cleanup deleted", async () => {
    const context = await browser.newContext();
    const page = await context.newPage();
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));

    await page.goto(`${origin}/`, { waitUntil: "load" });
    await page.evaluate(async () => {
      await navigator.serviceWorker.register("service-worker.js", { scope: "./" });
      await navigator.serviceWorker.ready;
    });
    const active = await snapshotName(build);
    await waitForCache(page, active);
    await page.waitForFunction(() => navigator.serviceWorker.controller !== null, undefined, {
      timeout: 10_000,
    });
    // The activation runs the first cleanup. Wait for it to end, so the pause
    // below belongs to the cleanup this test starts.
    await waitForScans(page, 1);

    // An install that stopped leaves its record behind. The record is older
    // than the limit, so the next cleanup classifies the snapshot as unused.
    const orphan = `cmsc127-harness-${"b".repeat(64)}`;
    await page.evaluate(async (name) => {
      const cache = await caches.open(name);
      await cache.put(new URL("__building__", location.href).href, new Response("0"));
    }, orphan);
    expect(await page.evaluate((name) => caches.has(name), orphan)).toBe(true);

    // The first cleanup reads its list and then holds.
    holdScan = true;
    const first = cleanup(page);
    let held = false;
    for (let step = 0; step < 100 && !held; step += 1) {
      held = await Promise.race([
        scanHeld.then(() => true),
        page.waitForTimeout(100).then(() => false),
      ]);
    }
    expect(held).toBe(true);

    // The second cleanup runs to its end in that window.
    await cleanup(page);
    expect(await page.evaluate((name) => caches.has(name), orphan)).toBe(false);

    // The first cleanup now acts on the list it took before the delete.
    releaseScan();
    await first;

    // A third pass shows what the first cleanup left behind.
    await cleanup(page);
    const names = await caches(page);
    const empty = await page.evaluate(async (list) => {
      const answer: string[] = [];
      for (const name of list) {
        if ((await (await caches.open(name)).keys()).length === 0) answer.push(name);
      }
      return answer;
    }, names);

    // State the defect in the failure message. The first cleanup planted the
    // snapshot that the second cleanup deleted. The third pass kept it.
    expect({ kept: names.includes(orphan), empty }).toEqual({ kept: false, empty: [] });
    expect(names).toContain(active);
    expect(errors).toEqual([]);

    await context.close();
  }, 120_000);
});
