import { spawn } from "node:child_process";
import { setTimeout as delay } from "node:timers/promises";
import { chromium } from "playwright";

const expectedPanels = ["Tables", "Changes", "Schema", "Sessions", "Checks", "Roles", "Status"];
const python = process.platform === "win32" ? ".venv\\Scripts\\python.exe" : ".venv/bin/python";
const server = spawn(python, ["dist/bundle/server.py", "--no-browser"], {
  cwd: process.cwd(),
  env: { ...process.env, CMSC127_PORT: "55442" },
  stdio: ["ignore", "pipe", "pipe"],
  windowsHide: true,
});

let serverOutput = "";
server.stdout.on("data", (chunk) => {
  serverOutput += chunk.toString();
});
server.stderr.on("data", (chunk) => {
  serverOutput += chunk.toString();
});

let browser;

try {
  const startupDeadline = Date.now() + 15_000;
  let address;
  while (Date.now() < startupDeadline) {
    const match = /CMSC 127 Harness is available at (http:\/\/127\.0\.0\.1:\d+\/)/.exec(serverOutput);
    if (match?.[1]) {
      address = match[1];
      break;
    }
    if (server.exitCode !== null) {
      break;
    }
    await delay(50);
  }
  if (!address) {
    throw new Error(`The harness did not print its address. ${serverOutput}`);
  }

  browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();
  const browserErrors = [];
  page.on("console", (message) => {
    if (message.type() === "error") {
      browserErrors.push(message.text());
    }
  });
  page.on("pageerror", (error) => browserErrors.push(error.message));

  await page.goto(address, { waitUntil: "domcontentloaded" });
  await page.waitForFunction(
    () => document.querySelectorAll("#panels button").length === 7,
    undefined,
    { timeout: 10_000 },
  );
  const panelLabels = await page.locator("#panels button").allTextContents();
  if (JSON.stringify(panelLabels) !== JSON.stringify(expectedPanels)) {
    throw new Error(`Unexpected panels: ${JSON.stringify(panelLabels)}`);
  }

  await page.waitForFunction(
    () => document.querySelector("#poll-summary")?.textContent?.startsWith("Last table poll:")
      || document.querySelector("#connection-message")?.textContent?.length,
    undefined,
    { timeout: 15_000 },
  );
  const pollSummary = await page.locator("#poll-summary").textContent();
  if (!pollSummary?.startsWith("Last table poll:")) {
    const message = await page.locator("#connection-message").textContent();
    throw new Error(`The page did not connect to PostgreSQL. ${message ?? "No status message."}`);
  }

  for (const panel of expectedPanels) {
    const button = page.getByRole("button", { name: panel, exact: true });
    await button.click();
    if (await button.getAttribute("aria-pressed") !== "true") {
      throw new Error(`The ${panel} tab did not become active.`);
    }
  }
  await page.waitForTimeout(1_100);
  if (browserErrors.length > 0) {
    throw new Error(`Browser errors: ${browserErrors.join(" | ")}`);
  }

  console.log(`Browser smoke passed. Rendered ${panelLabels.length} tabs. No console or page errors.`);
} finally {
  try {
    if (browser) {
      await browser.close();
    }
  } finally {
    if (server.exitCode === null) {
      const stopped = new Promise((resolve) => server.once("exit", resolve));
      server.kill();
      await stopped;
    }
  }
}
