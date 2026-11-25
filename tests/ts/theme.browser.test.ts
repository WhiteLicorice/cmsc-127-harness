import { createServer, type Server } from "node:http";
import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import { chromium, type Browser, type BrowserContext } from "playwright";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { closeServer } from "./listen";

const root = resolve(import.meta.dirname, "../..");
const publicRoot = resolve(root, "public");

const STORAGE_KEY = "cmsc127-theme";

let server: Server;
let browser: Browser;
let baseUrl = "";

async function serve(requestPath: string): Promise<{ body: Buffer; type: string } | null> {
  const name = requestPath === "/" ? "index.html" : requestPath.slice(1);
  if (name !== "index.html" && name !== "style.css") {
    return null;
  }
  const body = await readFile(resolve(publicRoot, name));
  const type = name.endsWith(".css") ? "text/css" : "text/html";
  return { body, type };
}

beforeAll(async () => {
  server = createServer((request, response) => {
    void serve(request.url ?? "/").then((file) => {
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
    throw new Error("The theme test server did not bind to a port.");
  }
  baseUrl = `http://127.0.0.1:${address.port}/`;
  browser = await chromium.launch({ headless: true });
}, 60_000);

afterAll(async () => {
  await browser?.close();
  await closeServer(server);
});

async function openPage(context: BrowserContext): Promise<import("playwright").Page> {
  const page = await context.newPage();
  await page.goto(baseUrl, { waitUntil: "load" });
  return page;
}

describe("theme picker", () => {
  it("keeps the chosen theme after a reload", async () => {
    const context = await browser.newContext({ colorScheme: "light" });
    try {
      const page = await openPage(context);
      await page.selectOption("#theme-select", "nord");
      expect(await page.evaluate(() => document.documentElement.dataset.theme)).toBe("nord");
      expect(await page.evaluate((key) => localStorage.getItem(key), STORAGE_KEY)).toBe("nord");

      await page.reload({ waitUntil: "load" });
      expect(await page.evaluate(() => document.documentElement.dataset.theme)).toBe("nord");
      expect(await page.inputValue("#theme-select")).toBe("nord");
      const background = await page.evaluate(() => getComputedStyle(document.body).backgroundColor);
      expect(background).toBe("rgb(46, 52, 64)");
    } finally {
      await context.close();
    }
  }, 60_000);

  it("applies a theme change without a reload", async () => {
    const context = await browser.newContext({ colorScheme: "light" });
    try {
      const page = await openPage(context);
      await page.evaluate(() => {
        (window as unknown as { marker: string }).marker = "kept";
      });
      await page.selectOption("#theme-select", "gruvbox-dark");
      expect(await page.evaluate(() => (window as unknown as { marker?: string }).marker)).toBe("kept");
      const background = await page.evaluate(() => getComputedStyle(document.body).backgroundColor);
      expect(background).toBe("rgb(40, 40, 40)");
    } finally {
      await context.close();
    }
  }, 60_000);

  it("opens UPV Dark on a clean profile with a dark system preference", async () => {
    const context = await browser.newContext({ colorScheme: "dark" });
    try {
      const page = await openPage(context);
      expect(await page.evaluate(() => localStorage.getItem("cmsc127-theme"))).toBeNull();
      expect(await page.evaluate(() => document.documentElement.dataset.theme)).toBe("upv-dark");
      expect(await page.inputValue("#theme-select")).toBe("upv-dark");
      expect(await page.evaluate(() => getComputedStyle(document.body).backgroundColor)).toBe(
        "rgb(23, 17, 15)",
      );
    } finally {
      await context.close();
    }
  }, 60_000);

  it("opens UPV Light on a clean profile with a light system preference", async () => {
    const context = await browser.newContext({ colorScheme: "light" });
    try {
      const page = await openPage(context);
      expect(await page.evaluate(() => document.documentElement.dataset.theme)).toBe("upv-light");
    } finally {
      await context.close();
    }
  }, 60_000);

  it("sets the theme before the document finishes parsing, so nothing flashes", async () => {
    const context = await browser.newContext({ colorScheme: "dark" });
    try {
      const page = await context.newPage();
      await page.addInitScript(() => {
        document.addEventListener("DOMContentLoaded", () => {
          (window as unknown as { themeAtParse?: string }).themeAtParse =
            document.documentElement.getAttribute("data-theme") ?? "";
        });
      });
      await page.goto(baseUrl, { waitUntil: "load" });
      expect(await page.evaluate(() => (window as unknown as { themeAtParse?: string }).themeAtParse)).toBe(
        "upv-dark",
      );
    } finally {
      await context.close();
    }
  }, 60_000);

  it("keeps the header within 360 px", async () => {
    const context = await browser.newContext({ colorScheme: "light", viewport: { width: 360, height: 800 } });
    try {
      const page = await openPage(context);
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - 360);
      expect(overflow).toBeLessThanOrEqual(0);
      for (const selector of ["h1", ".byline", "#theme-select", "#schema-select"]) {
        const box = await page.locator(selector).boundingBox();
        expect(box, selector).not.toBeNull();
        expect(box?.x ?? 0, selector).toBeGreaterThanOrEqual(0);
        expect((box?.x ?? 0) + (box?.width ?? 0), selector).toBeLessThanOrEqual(360);
      }
    } finally {
      await context.close();
    }
  }, 60_000);
});
