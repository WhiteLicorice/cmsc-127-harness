// Every themed button paints from the page's own tokens.
//
// The Status panel holds the Connect button, and the page renders it only on the
// hosted origin, so this test loads the hosted page from the same files and
// switches themes through the picker. For each theme it reads the computed
// style of the button in its base, hover, focus-visible, and disabled states.
// The background, text, and border colours must equal the theme's tokens, the
// font must equal the body font, and every text colour must clear 4.5:1 against
// the background painted under it.
import { createServer, type Server } from "node:http";
import { readFile } from "node:fs/promises";
import { extname, resolve } from "node:path";
import { chromium, type Browser, type BrowserContext, type Page } from "playwright";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { closeServer } from "./listen";

const root = resolve(import.meta.dirname, "../..");
const publicRoot = resolve(root, "public");
const HOSTED_ORIGIN = "https://whitelicorice.github.io";
const HOSTED_PAGE = `${HOSTED_ORIGIN}/cmsc-127-harness/`;
const THEMES = [
  "upv-light", "upv-dark", "github-light", "github-dark", "one-dark", "nord",
  "solarized-light", "solarized-dark", "catppuccin-latte", "catppuccin-mocha",
  "tokyo-night", "gruvbox-light", "gruvbox-dark",
];

const TYPES: Record<string, string> = {
  ".html": "text/html", ".css": "text/css", ".js": "text/javascript",
};

let server: Server;
let browser: Browser;
let baseUrl = "";

async function pageFile(pathname: string): Promise<{ body: Buffer; type: string } | null> {
  if (pathname === "/service-worker.js") {
    return { body: Buffer.from("self.addEventListener('install', () => self.skipWaiting());\n"), type: "text/javascript" };
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
    throw new Error("The button theme test server did not bind to a port.");
  }
  baseUrl = `http://127.0.0.1:${address.port}/`;
  browser = await chromium.launch({ headless: true });
}, 60_000);

afterAll(async () => {
  await browser?.close();
  await closeServer(server);
});

async function openHosted(context: BrowserContext): Promise<Page> {
  const page = await context.newPage();
  await context.route(`${HOSTED_ORIGIN}/**`, async (route) => {
    const file = await pageFile(new URL(route.request().url()).pathname);
    if (file === null) {
      await route.fulfill({ status: 404, body: "missing" });
      return;
    }
    await route.fulfill({ status: 200, contentType: file.type, body: file.body });
  });
  await page.goto(HOSTED_PAGE, { waitUntil: "domcontentloaded" });
  await page.waitForFunction(
    () => document.querySelector("[data-action='connect']") !== null,
    undefined,
    { timeout: 15_000 },
  );
  return page;
}

type Paint = {
  background: string;
  color: string;
  border: string;
  fontFamily: string;
  fontSize: string;
  outline: string;
  outlineWidth: string;
};

// One round trip: set the theme, wait for the page to repaint the panel, and
// read the token values beside the button's computed style.
async function paint(page: Page, theme: string): Promise<{ paint: Paint; tokens: Record<string, string>; body: string }> {
  await page.selectOption("#theme-select", theme);
  await page.waitForFunction(
    (value) => document.documentElement.dataset.theme === value
      && document.querySelector("[data-action='connect']") !== null,
    theme,
    { timeout: 15_000 },
  );
  return await page.evaluate(() => {
    const button = document.querySelector("[data-action='connect']");
    if (!(button instanceof HTMLButtonElement)) {
      throw new Error("The Connect button is missing.");
    }
    const probe = (property: "background" | "color", token: string): string => {
      const element = document.createElement("div");
      element.style.setProperty(property, `var(${token})`);
      document.body.append(element);
      const value = property === "background"
        ? getComputedStyle(element).backgroundColor
        : getComputedStyle(element).color;
      element.remove();
      return value;
    };
    const style = getComputedStyle(button);
    return {
      paint: {
        background: style.backgroundColor,
        color: style.color,
        border: style.borderTopColor,
        fontFamily: style.fontFamily,
        fontSize: style.fontSize,
        outline: style.outlineColor,
        outlineWidth: style.outlineWidth,
      },
      tokens: {
        "surface": probe("background", "--surface"),
        "surface-2": probe("background", "--surface-2"),
        "bg": probe("background", "--bg"),
        "text": probe("background", "--text"),
        "border": probe("background", "--border"),
        "rule": probe("background", "--rule"),
        "accent": probe("background", "--accent"),
        "focus": probe("background", "--focus"),
      },
      body: getComputedStyle(document.body).fontFamily,
    };
  });
}

function channels(value: string): [number, number, number] {
  const numbers = /(\d+(?:\.\d+)?)/g;
  const parts = [...value.matchAll(numbers)].map((match) => Number(match[1]));
  return [parts[0] ?? 0, parts[1] ?? 0, parts[2] ?? 0];
}

function luminance(value: string): number {
  const lin = (channel: number): number => {
    const scaled = channel / 255;
    return scaled <= 0.03928 ? scaled / 12.92 : ((scaled + 0.055) / 1.055) ** 2.4;
  };
  const [r, g, b] = channels(value);
  return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
}

function contrast(first: string, second: string): number {
  const a = luminance(first);
  const b = luminance(second);
  return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
}

describe("themed buttons", () => {
  it("paints every Connect button from its theme tokens", async () => {
    expect(baseUrl.startsWith("http://127.0.0.1:")).toBe(true);
    const context = await browser.newContext({ colorScheme: "light", viewport: { width: 1280, height: 900 } });
    try {
      const page = await openHosted(context);
      const ratios: string[] = [];
      for (const theme of THEMES) {
        const { paint: base, tokens, body } = await paint(page, theme);

        // The base state: surface-2 behind the text, the body text colour, and
        // the theme border. The font is the page font at the page size.
        expect(base.background, `${theme} base background`).toBe(tokens["surface-2"]);
        expect(base.color, `${theme} base text`).toBe(tokens["text"]);
        expect(base.border, `${theme} base border`).toBe(tokens["border"]);
        expect(base.fontFamily, `${theme} base font family`).toBe(body);
        expect(base.fontSize, `${theme} base font size`).toBe(
          await page.evaluate(() => getComputedStyle(document.body).fontSize),
        );
        ratios.push(`${theme} base ${contrast(base.color, base.background).toFixed(2)}`);

        // Hover: the accent paints the background and the page background
        // colour carries the text.
        const button = page.locator("[data-action='connect']");
        await button.hover();
        await page.waitForTimeout(50);
        const hover = await page.evaluate(() => {
          const style = getComputedStyle(document.querySelector("[data-action='connect']") as Element);
          return { background: style.backgroundColor, color: style.color, border: style.borderTopColor };
        });
        expect(hover.background, `${theme} hover background`).toBe(tokens["accent"]);
        expect(hover.color, `${theme} hover text`).toBe(tokens["bg"]);
        expect(hover.border, `${theme} hover border`).toBe(tokens["accent"]);
        ratios.push(`${theme} hover ${contrast(hover.color, hover.background).toFixed(2)}`);

        // Focus-visible: the theme ring. The mouse leaves the button first, so
        // the ring is read against the base paint.
        await page.mouse.move(0, 0);
        let focused = false;
        for (let step = 0; step < 40; step += 1) {
          await page.keyboard.press("Tab");
          focused = await page.evaluate(() => document.activeElement?.getAttribute("data-action") === "connect");
          if (focused) break;
        }
        expect(focused, `${theme} focused the Connect button`).toBe(true);
        const ring = await page.evaluate(() => {
          const style = getComputedStyle(document.querySelector("[data-action='connect']") as Element);
          return { outline: style.outlineColor, width: style.outlineWidth, background: style.backgroundColor, color: style.color };
        });
        expect(ring.outline, `${theme} focus ring`).toBe(tokens["focus"]);
        expect(ring.width, `${theme} focus ring width`).toBe("3px");
        expect(ring.background, `${theme} focus background`).toBe(tokens["surface-2"]);
        expect(ring.color, `${theme} focus text`).toBe(tokens["text"]);

        // Disabled: the base paint stays readable, and the border drops to the
        // rule token so the button reads as unavailable.
        const disabled = await page.evaluate(() => {
          const element = document.querySelector("[data-action='connect']") as HTMLButtonElement;
          element.disabled = true;
          const style = getComputedStyle(element);
          return { background: style.backgroundColor, color: style.color, border: style.borderTopColor };
        });
        expect(disabled.background, `${theme} disabled background`).toBe(tokens["surface-2"]);
        expect(disabled.color, `${theme} disabled text`).toBe(tokens["text"]);
        expect(disabled.border, `${theme} disabled border`).toBe(tokens["rule"]);
        ratios.push(`${theme} disabled ${contrast(disabled.color, disabled.background).toFixed(2)}`);

        for (const state of ["base", "hover", "disabled"]) {
          const line = ratios.find((entry) => entry.startsWith(`${theme} ${state} `));
          const value = Number(line?.split(" ").at(-1));
          expect(value, `${theme} ${state} contrast ${line}`).toBeGreaterThanOrEqual(4.5);
        }
      }
      console.log(ratios.join("\n"));
    } finally {
      await context.close();
    }
  }, 180_000);
});
