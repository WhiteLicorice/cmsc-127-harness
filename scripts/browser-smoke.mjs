import { spawn, spawnSync } from "node:child_process";
import { mkdir, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { chromium } from "playwright";

const expectedPanels = ["Tables", "Changes", "Schema", "Sessions", "Checks", "Roles", "Status"];
const python = resolve(process.platform === "win32" ? ".venv\\Scripts\\python.exe" : ".venv/bin/python");
const projectRoot = resolve("smoke-project");
const artifactRoot = resolve("smoke-artifacts");
const serverScript = resolve("dist/bundle/server.py");

if (!process.env["CMSC127_DATABASE_URL"]) {
  console.error("Set CMSC127_DATABASE_URL to a disposable database before the browser smoke.");
  process.exit(1);
}

function runSql(statements) {
  const program = [
    "import os",
    "import psycopg",
    "with psycopg.connect(os.environ['CMSC127_DATABASE_URL'], autocommit=True) as connection:",
    ...statements.map((statement) => `    connection.execute(${JSON.stringify(statement)})`),
  ].join("\n");
  const completed = spawnSync(python, ["-c", program], { stdio: "inherit" });
  if (completed.status !== 0) {
    throw new Error("The smoke fixture SQL failed.");
  }
}

const seedSql = [
  "CREATE TABLE IF NOT EXISTS public.smoke_student (id integer PRIMARY KEY, name text NOT NULL)",
  "TRUNCATE public.smoke_student",
  "INSERT INTO public.smoke_student VALUES (1, 'Ana'), (2, 'Ben')",
];
runSql(seedSql);

await mkdir(projectRoot, { recursive: true });
await mkdir(artifactRoot, { recursive: true });
await writeFile(
  resolve(projectRoot, "README.md"),
  "```mermaid\nerDiagram\n  smoke_student {\n    int id PK\n    text name\n  }\n```\n",
  "utf-8",
);

const log = [];
function record(line) {
  log.push(line);
}

const server = spawn(python, [serverScript, "--no-browser"], {
  cwd: projectRoot,
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
let failure = null;

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
  record(`address: ${address}`);

  browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
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
  record(`panels: ${panelLabels.join(", ")}`);

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
  record(`poll: ${pollSummary}`);

  const tablesText = await page.locator("#panel-content").textContent();
  if (!tablesText?.includes("smoke_student") || !tablesText?.includes("Ana")) {
    throw new Error(`The Tables panel did not render the seeded rows: ${tablesText ?? ""}`);
  }
  await page.screenshot({ path: resolve(artifactRoot, "01-tables.png"), fullPage: true });

  let panelNumber = 2;
  let schemaSvgs = [];
  for (const panel of expectedPanels.slice(1)) {
    const button = page.getByRole("button", { name: panel, exact: true });
    await button.click();
    if (await button.getAttribute("aria-pressed") !== "true") {
      throw new Error(`The ${panel} tab did not become active.`);
    }
    await page.waitForTimeout(panel === "Schema" ? 1_500 : 250);
    if (panel === "Schema") {
      schemaSvgs = await page.locator(".mermaid").evaluateAll((nodes) =>
        nodes.map((node) => node.querySelectorAll("svg").length),
      );
    }
    const name = `${String(panelNumber).padStart(2, "0")}-${panel.toLowerCase()}.png`;
    panelNumber += 1;
    await page.screenshot({ path: resolve(artifactRoot, name), fullPage: true });
  }

  if (JSON.stringify(schemaSvgs) !== JSON.stringify([1, 1])) {
    throw new Error(`The Schema panel did not draw both diagrams: ${JSON.stringify(schemaSvgs)}`);
  }
  record(`schema diagrams: ${JSON.stringify(schemaSvgs)}`);

  if (browserErrors.length > 0) {
    throw new Error(`Browser errors: ${browserErrors.join(" | ")}`);
  }
  record(`Browser smoke passed. Rendered ${panelLabels.length} tabs and both schema diagrams. No console or page errors.`);
} catch (error) {
  failure = error instanceof Error ? error : new Error(String(error));
  record(`FAILED: ${failure.message}`);
  if (browser) {
    const pages = browser.contexts().flatMap((context) => context.pages());
    const page = pages[0];
    if (page) {
      try {
        await page.screenshot({ path: resolve(artifactRoot, "failure.png"), fullPage: true });
      } catch {
        record("The failure screenshot could not be written.");
      }
    }
  }
} finally {
  record(`os: ${process.platform} ${process.arch}`);
  record(`node: ${process.version}`);
  if (browser) {
    record(`chromium: ${browser.version()}`);
  }
  record(`run: ${process.env.GITHUB_RUN_ID ?? "local"} attempt ${process.env.GITHUB_RUN_ATTEMPT ?? "local"}`);
  record(`commit: ${process.env.GITHUB_SHA ?? "local"}`);
  record(`server output: ${serverOutput.trim() || "(none)"}`);
  await writeFile(resolve(artifactRoot, "smoke.log"), `${log.join("\n")}\n`, "utf-8");
  console.log(log.join("\n"));

  try {
    if (browser) {
      await browser.close();
    }
  } finally {
    if (server.exitCode === null) {
      const stopped = new Promise((resolveExit) => server.once("exit", resolveExit));
      server.kill();
      await stopped;
    }
  }
  runSql(["DROP TABLE IF EXISTS public.smoke_student"]);
}

if (failure) {
  process.exitCode = 1;
}
