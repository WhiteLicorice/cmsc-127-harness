import { createServer, type Server } from "node:http";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { extname, join, resolve } from "node:path";
import { chromium, type Browser, type Page } from "playwright";
import { afterAll, beforeAll, describe, expect, it } from "vitest";

// A rejected update keeps the first snapshot. The next navigation then meets
// HTML from the second build. The worker must answer with the page that
// belongs to the snapshot, so one build's HTML never runs another build's
// scripts. This test serves two real builds and reloads the page.

const root = resolve(import.meta.dirname, "../..");
const fixtureRoot = resolve(root, ".vitest/two-build");

// The page tests bind port 0 on loopback, so two lanes never collide. The test
// records the port it took in its own output.
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

// Write one real build with the offline build script, so the manifest and the
// worker carry the same ID and every hash as the release does.
async function writeBuild(name: string, html: string, app: string): Promise<Build> {
  const directory = resolve(fixtureRoot, name);
  await mkdir(directory, { recursive: true });
  await writeFile(join(directory, "index.html"), html, "utf-8");
  await writeFile(join(directory, "app.js"), app, "utf-8");
  const { writeOfflineFiles } = await import("../../scripts/build-offline.mjs");
  await writeOfflineFiles(directory);
  return { directory };
}

function buildIdOf(manifest: string): string {
  return manifest.match(/"buildId":"([0-9a-f]{64})"/)?.[1] ?? "";
}

async function snapshotName(build: Build): Promise<string> {
  const manifest = await readFile(join(build.directory, "offline-manifest.json"), "utf-8");
  return `cmsc127-harness-${buildIdOf(manifest)}`;
}

let buildA: Build;
let buildB: Build;
let server: Server;
let browser: Browser;
let origin = "";
let served: Build;
let corruptApp: string | null = null;

// The worker and every asset are no-store, so the HTTP cache never answers for
// the server. The test states the worker's behavior, not the cache's.
function serveFile(response: import("node:http").ServerResponse, body: Buffer, type: string): void {
  response.writeHead(200, { "Content-Type": type, "Cache-Control": "no-store" });
  response.end(body);
}

beforeAll(async () => {
  buildA = await writeBuild("build-a", PAGE_A, APP_A);
  buildB = await writeBuild("build-b", PAGE_B, APP_B);
  served = buildA;
  server = createServer((request, response) => {
    const pathname = new URL(request.url ?? "/", "http://127.0.0.1").pathname;
    const relative = pathname === "/" ? "index.html" : pathname.slice(1);
    if (relative === "app.js" && corruptApp !== null) {
      serveFile(response, Buffer.from(corruptApp, "utf-8"), "text/javascript");
      return;
    }
    const file = join(served.directory, relative);
    void readFile(file).then(
      (body) => serveFile(response, body, TYPES[extname(file)] ?? "application/octet-stream"),
      () => {
        response.writeHead(404);
        response.end();
      },
    );
  });
  await new Promise<void>((ready) => server.listen(0, "127.0.0.1", ready));
  const address = server.address();
  if (address === null || typeof address === "string") {
    throw new Error("The two-build test server did not bind to a port.");
  }
  origin = `http://127.0.0.1:${address.port}`;
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
  for (let step = 0; step < 100; step += 1) {
    if ((await caches(page)).includes(name)) return;
    await page.waitForTimeout(100);
  }
  throw new Error(`The snapshot ${name} never appeared.`);
}

// Activation deletes the older snapshots. A reload before it would still meet
// the old worker, so the test waits for the deletion.
async function waitForOnlyCache(page: Page, name: string): Promise<void> {
  for (let step = 0; step < 100; step += 1) {
    if ((await caches(page)).join() === name) return;
    await page.waitForTimeout(100);
  }
  throw new Error(`The snapshot ${name} did not become the only one.`);
}

async function update(page: Page): Promise<void> {
  await page.evaluate(async () => {
    const registration = await navigator.serviceWorker.getRegistration();
    await registration?.update();
  });
}

describe("two builds", () => {
  it("serves the active page after a rejected update", async () => {
    const errors: string[] = [];
    const context = await browser.newContext();
    const page = await context.newPage();
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

    // The server offers build B with a damaged app. The browser rejects the
    // install and keeps the build A snapshot.
    served = buildB;
    corruptApp = `${APP_B}\n// one byte the manifest does not name\n`;
    await update(page);
    for (let step = 0; step < 100; step += 1) {
      if ((await caches(page)).length === 1) break;
      await page.waitForTimeout(100);
    }
    expect(await caches(page)).toEqual([cacheA]);

    // A real reload meets the build B document from the network, and the worker
    // answers with the build A document that its scripts belong to.
    errors.length = 0;
    await page.reload({ waitUntil: "load" });
    expect(errors).toEqual([]);
    expect(await page.evaluate(() => document.body.dataset["build"])).toBe("a");
    expect(await page.evaluate(() => document.getElementById("panel-a") !== null)).toBe(true);
    expect(await page.evaluate(() => document.getElementById("panel-b") !== null)).toBe(false);

    // With the verified bytes, the next update installs and the next reload
    // runs the new build.
    corruptApp = null;
    await update(page);
    const cacheB = await snapshotName(buildB);
    await waitForOnlyCache(page, cacheB);
    await page.reload({ waitUntil: "load" });
    expect(await page.evaluate(() => document.body.dataset["build"])).toBe("b");

    await context.close();
  }, 120_000);
});
