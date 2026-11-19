import { cp, mkdir, readFile } from "node:fs/promises";
import { extname, join, resolve } from "node:path";
import { chromium, type Browser, type BrowserContext, type Page } from "playwright";
import { afterAll, beforeAll, describe, expect, it } from "vitest";

// The Status panel shows the offline line only after a complete snapshot is
// verified and stored. A load that draws before the snapshot download finishes
// must show nothing about offline use.
//
// This test copies the built page, writes the worker files the release writes,
// and serves the copy under the hosted origin. The page then registers its own
// worker, and route interception sees the worker's own requests.

const root = resolve(import.meta.dirname, "../..");
const publicRoot = resolve(root, "public");
const buildRoot = resolve(root, ".vitest/offline-claim");
const HOSTED_ORIGIN = "https://whitelicorice.github.io";
const PAGE = `${HOSTED_ORIGIN}/cmsc-127-harness/`;
const OFFLINE_SAVED = "Saved for offline use.";

const TYPES: Record<string, string> = {
  ".html": "text/html", ".css": "text/css", ".js": "text/javascript",
  ".json": "application/json", ".txt": "text/plain",
};

let browser: Browser;

async function writeFixture(): Promise<void> {
  await mkdir(buildRoot, { recursive: true });
  await cp(publicRoot, buildRoot, { recursive: true });
  const { writeOfflineFiles } = await import("../../scripts/build-offline.mjs");
  await writeOfflineFiles(buildRoot);
}

async function fileFor(pathname: string): Promise<{ body: Buffer; type: string } | null> {
  const relative = pathname === "/" || pathname === "/cmsc-127-harness/"
    ? "index.html"
    : pathname.replace(/^\/cmsc-127-harness\//, "").replace(/^\//, "");
  const file = resolve(buildRoot, relative);
  if (!file.startsWith(buildRoot)) {
    return null;
  }
  try {
    return { body: await readFile(file), type: TYPES[extname(file)] ?? "application/octet-stream" };
  } catch {
    return null;
  }
}

// A fresh profile with the page files routed and the manifest request under the
// caller's control. Route interception also sees the worker's own requests.
async function openHosted(
  blockManifest: boolean,
): Promise<{ page: Page; context: BrowserContext; manifestRequests: () => number }> {
  const context = await browser.newContext();
  await context.grantPermissions(["local-network-access"], { origin: HOSTED_ORIGIN });
  let blocked = 0;
  await context.route(`${HOSTED_ORIGIN}/**`, async (route) => {
    const pathname = new URL(route.request().url()).pathname;
    if (blockManifest && pathname.endsWith("/offline-manifest.json")) {
      blocked += 1;
      await route.fulfill({ status: 503, contentType: "text/plain", body: "blocked" });
      return;
    }
    const file = await fileFor(pathname);
    if (file === null) {
      await route.fulfill({ status: 404, body: "missing" });
      return;
    }
    await route.fulfill({ status: 200, contentType: file.type, body: file.body });
  });
  const page = await context.newPage();
  page.setDefaultTimeout(15_000);
  await page.goto(PAGE, { waitUntil: "domcontentloaded" });
  await page.getByRole("button", { name: "Connect", exact: true }).waitFor();
  return { page, context, manifestRequests: () => blocked };
}

async function panelText(page: Page): Promise<string> {
  return page.locator("#panel-content").innerText();
}

async function waitForText(page: Page, text: string, timeout = 20_000): Promise<boolean> {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if ((await panelText(page)).includes(text)) return true;
    await page.waitForTimeout(250);
  }
  return false;
}

beforeAll(async () => {
  await writeFixture();
  browser = await chromium.launch({ headless: true });
}, 120_000);

afterAll(async () => {
  await browser?.close();
});

describe("the offline claim", () => {
  it("never shows the offline line when the first manifest request fails", async () => {
    const { page, context, manifestRequests } = await openHosted(true);
    try {
      // The page drew, and the install stopped on the manifest.
      await page.waitForTimeout(2000);
      expect(manifestRequests()).toBeGreaterThan(0);
      expect(await page.evaluate(() => caches.keys())).toEqual([]);
      expect(await panelText(page)).not.toContain(OFFLINE_SAVED);
      expect(await waitForText(page, OFFLINE_SAVED, 3000)).toBe(false);
    } finally {
      await context.close();
    }
  }, 120_000);

  it("shows the offline line once the first snapshot is stored", async () => {
    const { page, context } = await openHosted(false);
    try {
      expect(await waitForText(page, OFFLINE_SAVED)).toBe(true);
      const missing = await page.evaluate(async () => {
        const names = (await caches.keys()).filter((name) => name.startsWith("cmsc127-harness-"));
        for (const name of names) {
          const cache = await caches.open(name);
          const record = await cache.match(new URL("offline-manifest.json", location.href).href);
          if (!record) continue;
          const manifest = await record.json();
          if (!manifest.files?.length) continue;
          const gaps = [];
          for (const entry of manifest.files) {
            if (!(await cache.match(new URL(entry.path, location.href).href))) gaps.push(entry.path);
          }
          return gaps;
        }
        return ["no snapshot"];
      });
      expect(missing).toEqual([]);
    } finally {
      await context.close();
    }
  }, 120_000);
});
