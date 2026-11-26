import { spawn, spawnSync } from "node:child_process";
import { mkdir, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { chromium } from "playwright";

// A student opens the page on GitHub Pages, and the page reaches a server on
// this computer. This smoke reads the published page, then proves that the
// page reaches a real `server.py` on 127.0.0.1 and shows a row.
//
// The published page can be older than the commit under test. With
// CMSC127_EXPECT_BUILD set, the script polls `version.json` until the page
// reports that commit, so a pass cannot come from the old site. Without it,
// the script reports the stamp it sees. A dispatch uses that mode to test the
// site that is live now.
const DEFAULT_PAGE = "https://whitelicorice.github.io/cmsc-127-harness/";
const pageUrl = (process.env["CMSC127_HOSTED_URL"] ?? DEFAULT_PAGE).replace(/\/*$/, "/");
const expectedBuild = (process.env["CMSC127_EXPECT_BUILD"] ?? "").trim().toLowerCase();
const buildWaitMs = Number(process.env["CMSC127_BUILD_WAIT_MS"] ?? 600_000);
const python = resolve(process.platform === "win32" ? ".venv\\Scripts\\python.exe" : ".venv/bin/python");
const projectRoot = resolve("hosted-smoke-project");
const artifactRoot = resolve("smoke-artifacts", "hosted");
const serverScript = resolve("server.py");
// A gate that runs beside another gate reserves its own port. CMSC127_PORT
// sets that port. The value below is the one this script used before the port
// became a choice.
const port = process.env["CMSC127_PORT"] ?? "55442";
const table = "hosted_smoke";

if (!process.env["CMSC127_DATABASE_URL"]) {
  console.error("Set CMSC127_DATABASE_URL to a disposable database before the hosted smoke.");
  process.exit(1);
}
if (!Number.isFinite(buildWaitMs) || buildWaitMs < 0) {
  console.error("CMSC127_BUILD_WAIT_MS must be a number of milliseconds.");
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
    throw new Error("The hosted smoke fixture SQL failed.");
  }
}

const log = [];
function record(line) {
  log.push(line);
}

async function readStamp() {
  const url = new URL("version.json", pageUrl);
  url.searchParams.set("cachebust", String(Date.now()));
  try {
    const response = await fetch(url, { cache: "no-store", headers: { "cache-control": "no-cache" } });
    if (!response.ok) {
      return { error: `the page answered HTTP ${response.status}` };
    }
    const body = await response.json();
    const commit = typeof body?.commit === "string" ? body.commit.toLowerCase() : "";
    if (!/^[0-9a-f]{40}$/.test(commit)) {
      return { error: "the page has no commit field" };
    }
    return { commit };
  } catch (error) {
    return { error: error instanceof Error ? error.message : String(error) };
  }
}

// With an expected build, the page must report this commit before the smoke
// reads anything else. Without one, the script reports what the live page
// carries and carries on.
async function checkBuild() {
  if (expectedBuild === "") {
    const stamp = await readStamp();
    record(stamp.commit === undefined
      ? `build: not gated (${stamp.error})`
      : `build: not gated, the live page reports ${stamp.commit}`);
    return;
  }
  if (!/^[0-9a-f]{40}$/.test(expectedBuild)) {
    throw new Error(`CMSC127_EXPECT_BUILD holds ${JSON.stringify(expectedBuild)}, which is not a commit hash.`);
  }
  const deadline = Date.now() + buildWaitMs;
  let last = "no answer";
  while (Date.now() < deadline) {
    const stamp = await readStamp();
    if (stamp.commit === expectedBuild) {
      record(`build: the page reports ${stamp.commit}`);
      return;
    }
    last = stamp.commit ?? stamp.error;
    await delay(5_000);
  }
  throw new Error(
    `The published page did not report build ${expectedBuild} within ${Math.round(buildWaitMs / 1000)} seconds. `
    + `Last answer: ${last}.`,
  );
}

await mkdir(projectRoot, { recursive: true });
await mkdir(artifactRoot, { recursive: true });
await writeFile(
  resolve(projectRoot, "README.md"),
  "```mermaid\nerDiagram\n  hosted_smoke {\n    int id PK\n    text name\n  }\n```\n",
  "utf-8",
);

runSql([
  `CREATE TABLE IF NOT EXISTS public.${table} (id integer PRIMARY KEY, name text NOT NULL)`,
  `TRUNCATE public.${table}`,
  `INSERT INTO public.${table} VALUES (1, 'Ana'), (2, 'Ben')`,
]);

const server = spawn(python, [serverScript, "--no-browser"], {
  cwd: projectRoot,
  env: { ...process.env, CMSC127_PORT: port },
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
  let serverAddress;
  while (Date.now() < startupDeadline) {
    const match = /CMSC 127 Harness is available at (http:\/\/127\.0\.0\.1:\d+\/)/.exec(serverOutput);
    if (match?.[1]) {
      serverAddress = match[1];
      break;
    }
    if (server.exitCode !== null) {
      break;
    }
    await delay(50);
  }
  if (!serverAddress) {
    throw new Error(`The harness did not print its address. ${serverOutput}`);
  }
  record(`server: ${serverAddress}`);

  await checkBuild();
  record(`page: ${pageUrl}`);

  browser = await chromium.launch({ headless: true });
  const origin = new URL(pageUrl).origin;
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  // Chrome asks for loopback access before an HTTPS page may reach a local
  // port. Grant it, so the check tests the connection rather than a prompt.
  await context.grantPermissions(["local-network-access"], { origin });
  const page = await context.newPage();
  const browserErrors = [];
  page.on("console", (message) => {
    if (message.type() === "error") {
      browserErrors.push(message.text());
    }
  });
  page.on("pageerror", (error) => browserErrors.push(error.message));

  const address = new URL(serverAddress);
  await page.goto(`${pageUrl}?port=${address.port}`, { waitUntil: "domcontentloaded" });
  await page.waitForFunction(
    () => document.querySelectorAll("#panels button").length === 7,
    undefined,
    { timeout: 20_000 },
  );
  record(`panels: ${(await page.locator("#panels button").allTextContents()).join(", ")}`);

  const button = page.getByRole("button", { name: "Connect", exact: true });
  if (await button.count() !== 1) {
    throw new Error("The hosted page drew no Connect button in the Status panel.");
  }
  await button.click();

  await page.waitForFunction(
    () => (document.querySelector("#poll-summary")?.textContent ?? "").startsWith("Last table poll:"),
    undefined,
    { timeout: 20_000 },
  );
  record(`poll: ${await page.locator("#poll-summary").textContent()}`);

  await page.getByRole("button", { name: "Status", exact: true }).click();
  await page.waitForFunction(
    (name) => (document.querySelector("#panel-content")?.textContent ?? "").includes(name),
    "Last poll",
    { timeout: 10_000 },
  );
  const statusText = await page.locator("#panel-content").textContent();
  if (!statusText?.includes("Database") || !statusText?.includes("Last poll")) {
    throw new Error(`The Status panel did not connect: ${statusText ?? ""}`);
  }
  const message = page.locator("#connection-message");
  if (await message.isVisible()) {
    throw new Error(`The page reported a connection problem: ${await message.textContent()}`);
  }
  record(`status: ${statusText.replace(/\s+/g, " ").trim()}`);
  await page.screenshot({ path: resolve(artifactRoot, "01-status.png"), fullPage: true });

  await page.getByRole("button", { name: "Tables", exact: true }).click();
  await page.waitForFunction(
    (name) => (document.querySelector("#panel-content")?.textContent ?? "").includes(name),
    table,
    { timeout: 10_000 },
  );
  const tablesText = await page.locator("#panel-content").textContent();
  if (!tablesText?.includes(table) || !tablesText?.includes("Ana")) {
    throw new Error(`The Tables panel did not render the seeded rows: ${tablesText ?? ""}`);
  }
  record(`tables: ${table} with the seeded rows`);
  await page.screenshot({ path: resolve(artifactRoot, "02-tables.png"), fullPage: true });

  if (browserErrors.length > 0) {
    throw new Error(`Browser errors: ${browserErrors.join(" | ")}`);
  }
  record("Hosted smoke passed. The published page reached server.py and drew the row.");
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
  await writeFile(resolve(artifactRoot, "hosted.log"), `${log.join("\n")}\n`, "utf-8");
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
  runSql([`DROP TABLE IF EXISTS public.${table}`]);
}

if (failure) {
  process.exitCode = 1;
}
