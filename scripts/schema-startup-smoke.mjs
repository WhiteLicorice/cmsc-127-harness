import { spawn } from "node:child_process";
import { mkdir, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { chromium } from "playwright";

const pairDatabaseUrl = process.env["CMSC127_PAIR_DATABASE_URL"];
if (!pairDatabaseUrl) {
  console.error(
    "Set CMSC127_PAIR_DATABASE_URL to a database whose only schema is pair, with a student table.",
  );
  process.exit(1);
}

const python = resolve(process.platform === "win32" ? ".venv\\Scripts\\python.exe" : ".venv/bin/python");
const projectRoot = resolve("smoke-project-pair");
const artifactRoot = resolve("smoke-artifacts");
const serverScript = resolve("dist/bundle/server.py");

await mkdir(projectRoot, { recursive: true });
await mkdir(artifactRoot, { recursive: true });
await writeFile(
  resolve(projectRoot, "README.md"),
  "```mermaid\nerDiagram\n  student {\n    int id PK\n    text name\n  }\n```\n",
  "utf-8",
);

const log = [];
function record(line) {
  log.push(line);
}

const server = spawn(python, [serverScript, "--no-browser"], {
  cwd: projectRoot,
  env: { ...process.env, CMSC127_PORT: "55443", CMSC127_DATABASE_URL: pairDatabaseUrl },
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
    () => document.querySelectorAll("#schema-select option").length > 0,
    undefined,
    { timeout: 15_000 },
  );

  const options = await page.locator("#schema-select option").evaluateAll((nodes) =>
    nodes.map((node) => ({ value: node.value, selected: node.selected })),
  );
  const selectorDisabled = await page.locator("#schema-select").isDisabled();
  const pollSummary = await page.locator("#poll-summary").textContent();
  const tablesText = await page.locator("#panel-content").textContent();
  record(`options: ${JSON.stringify(options)}`);
  record(`poll: ${pollSummary}`);
  await page.screenshot({ path: resolve(artifactRoot, "schema-startup-tables.png"), fullPage: true });

  await page.getByRole("button", { name: "Schema", exact: true }).click();
  await page.waitForTimeout(1_500);
  await page.waitForFunction(
    () => {
      const nodes = [...document.querySelectorAll(".mermaid")];
      return nodes.length === 2 && nodes.every((node) => node.querySelector("svg") !== null);
    },
    undefined,
    { timeout: 10_000 },
  );
  const schemaNodes = await page.locator(".mermaid").evaluateAll((nodes) =>
    nodes.map((node) => ({
      svgs: node.querySelectorAll("svg").length,
      text: node.textContent ?? "",
    })),
  );
  const schemaSvgs = schemaNodes.map((node) => node.svgs);
  const schemaText = schemaNodes.map((node) => node.text);
  await page.screenshot({ path: resolve(artifactRoot, "schema-startup-schema.png"), fullPage: true });
  record(`schema diagrams: ${JSON.stringify(schemaSvgs)}`);

  if (JSON.stringify(options.map((option) => option.value)) !== JSON.stringify(["pair"])) {
    throw new Error(`The selector did not offer only pair: ${JSON.stringify(options)}`);
  }
  if (selectorDisabled) {
    throw new Error("The schema selector stayed disabled.");
  }
  if (!options[0]?.selected) {
    throw new Error("The pair option was not selected.");
  }
  if (!pollSummary?.startsWith("Last table poll:")) {
    throw new Error(`The page did not poll: ${pollSummary ?? "no poll summary"}`);
  }
  if (!tablesText?.includes("student") || !tablesText?.includes("Ana")) {
    throw new Error(`The Tables panel did not render the pair rows: ${tablesText ?? ""}`);
  }
  if (JSON.stringify(schemaSvgs) !== JSON.stringify([1, 1])) {
    throw new Error(`The Schema panel did not draw both diagrams: ${JSON.stringify(schemaSvgs)}`);
  }
  if (schemaText.some((text) => /Syntax error/i.test(text))) {
    throw new Error(`A schema diagram failed to render: ${JSON.stringify(schemaText)}`);
  }
  if (!schemaText.every((text) => text.includes("student"))) {
    throw new Error(`A schema diagram did not draw the expected entity: ${JSON.stringify(schemaText)}`);
  }
  if (browserErrors.length > 0) {
    throw new Error(`Browser errors: ${browserErrors.join(" | ")}`);
  }
  record("Schema startup smoke passed. The page started on a pair-only database and offered pair.");
} catch (error) {
  failure = error instanceof Error ? error : new Error(String(error));
  record(`FAILED: ${failure.message}`);
  if (browser) {
    const pages = browser.contexts().flatMap((context) => context.pages());
    const page = pages[0];
    if (page) {
      try {
        await page.screenshot({ path: resolve(artifactRoot, "schema-startup-failure.png"), fullPage: true });
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
  record(`run: ${process.env["GITHUB_RUN_ID"] ?? "local"} attempt ${process.env["GITHUB_RUN_ATTEMPT"] ?? "local"}`);
  record(`commit: ${process.env["GITHUB_SHA"] ?? "local"}`);
  record(`server output: ${serverOutput.trim() || "(none)"}`);
  await writeFile(resolve(artifactRoot, "schema-startup.log"), `${log.join("\n")}\n`, "utf-8");
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
}

if (failure) {
  process.exitCode = 1;
}
