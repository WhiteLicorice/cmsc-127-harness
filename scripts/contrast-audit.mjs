// Rendered-page contrast audit.
//
// The CSS test reads tokens. This script reads the page. It walks every element
// that paints text, in every theme and every view, and compares the computed
// colour with the backdrop really painted behind it. HTML backdrops come from
// the ancestor background chain. Anything inside an SVG takes its backdrop from
// the painted shapes under the point, because a <g> paints nothing.
//
// A closed <select> paints its selected option's text but the option has a 0x0
// box, so the walk measures the select itself: its own box, colour, and
// background, with the selected option's text. The open list is a native popup
// outside the DOM and is not measured.
//
// Every view also asserts what it must contain. A panel that finishes in an
// error state, an empty state it should not be in, or with fewer rows than the
// fixture seeds fails the run, so measuring nothing cannot pass as measuring
// something.
//
// Focus rings are walked in the initial view, where a scroll container is also
// focusable. Every focusable element must paint a ring in every theme, so a
// theme that drops its outline fails instead of silently contributing zero
// measurements.
import { spawn } from "node:child_process";
import { mkdir, unlink, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { chromium } from "playwright";

const THEMES = [
  "upv-light", "upv-dark", "github-light", "github-dark", "one-dark", "nord",
  "solarized-light", "solarized-dark", "catppuccin-latte", "catppuccin-mocha",
  "tokyo-night", "gruvbox-light", "gruvbox-dark",
];
const PORT = "55447";
const TABLE = "audit_student";
const EMPTY_SCHEMA = "audit_empty";
const projectRoot = resolve("audit-project");
const artifactRoot = resolve("audit-artifacts");
const checkResultPath = resolve(projectRoot, ".cmsc127", "last-run.json");
const python = resolve(process.platform === "win32" ? ".venv\\Scripts\\python.exe" : ".venv/bin/python");

// One pass, one fail, one skip, and one pass with a warning, so the Checks
// panel paints every result cell and the warning note.
const CHECK_RESULT = {
  results: [
    { id: "row_count", kind: "check", stage: 1, status: "pass" },
    { id: "column_type", kind: "check", stage: 1, status: "fail", expected: "integer", actual: "text" },
    { id: "index_present", kind: "check", stage: 2, status: "skip" },
    { id: "null_ratio", kind: "check", stage: 2, status: "pass", warnings: ["3 rows exceed the soft limit"] },
  ],
  passed: 2,
  failed: 1,
  skipped: 1,
  warnings: 1,
};

// Each view names the panel it shows and what must be on the page before it is
// measured. `min` is the floor for the whole page's text elements, so a panel
// that renders one error paragraph cannot pass as a measured panel.
const VIEWS = [
  {
    name: "initial",
    min: 20,
    waitSelector: "tbody tr",
    needs: { "tbody tr": 2, "thead th": 2 },
  },
  {
    name: "Tables",
    panel: "Tables",
    min: 20,
    waitSelector: "tbody tr",
    needs: { "tbody tr": 2, "thead th": 2 },
  },
  {
    name: "Changes",
    panel: "Changes",
    min: 12,
    waitAny: ["No changes observed yet", "Changes come from the one-second"],
    needs: { "p": 1 },
  },
  {
    name: "Schema",
    panel: "Schema",
    min: 30,
    waitSelector: ".mermaid svg",
    needs: { ".mermaid svg": 2, ".mermaid p": 12 },
  },
  {
    name: "Sessions",
    panel: "Sessions",
    min: 24,
    waitSelector: "tbody tr",
    needs: { "tbody tr": 1 },
  },
  {
    name: "Checks",
    panel: "Checks",
    min: 30,
    waitSelector: ".result-pass",
    needs: { "tbody tr": 4, ".result-pass": 1, ".result-fail": 1, ".result-skip": 1, ".warning-note": 1 },
  },
  {
    name: "Roles",
    panel: "Roles",
    min: 14,
    waitSelector: "tbody tr",
    needs: { "tbody tr": 1 },
  },
  {
    name: "Status",
    panel: "Status",
    min: 16,
    waitSelector: "dl dt",
    needs: { "dl dt": 4 },
  },
  {
    name: "Status disconnected",
    panel: "Status",
    min: 12,
    route: true,
    waitSelector: ".error-message",
    needs: { ".error-message": 1 },
    text: ["PostgreSQL is unavailable"],
  },
  {
    name: "Checks empty",
    panel: "Checks",
    min: 12,
    hideCheckResult: true,
    waitText: "No check result exists",
    needs: { "tbody tr": 0 },
    text: ["No check result exists"],
  },
  {
    name: "Tables empty",
    panel: "Tables",
    schema: EMPTY_SCHEMA,
    min: 12,
    waitText: "No tables or views exist in this schema.",
    needs: { "tbody tr": 0 },
    text: ["No tables or views exist in this schema."],
  },
  {
    name: "Roles empty",
    panel: "Roles",
    min: 12,
    waitText: "No tables or user roles exist in this schema.",
    needs: { "tbody tr": 0 },
    text: ["No tables or user roles exist in this schema."],
  },
];

const COUNTERS = [
  "tbody tr", "thead th", "p", "dl dt", ".result-pass", ".result-fail", ".result-skip",
  ".warning-note", ".error-message", ".mermaid svg", ".mermaid p",
];

if (!process.env["CMSC127_DATABASE_URL"]) {
  console.error("Set CMSC127_DATABASE_URL to a disposable database before the contrast audit.");
  process.exit(1);
}

function runSql(statements) {
  return new Promise((ready, fail) => {
    const child = spawn(python, ["-c", [
      "import os",
      "import psycopg",
      "with psycopg.connect(os.environ['CMSC127_DATABASE_URL'], autocommit=True) as connection:",
      ...statements.map((statement) => `    connection.execute(${JSON.stringify(statement)})`),
    ].join("\n")], { stdio: "inherit" });
    child.on("exit", (code) => (code === 0 ? ready() : fail(new Error("the audit fixture SQL failed"))));
  });
}

function auditPage(counters) {
  const parse = (value) => {
    if (!value || value === "none" || value === "transparent") return null;
    const match = /rgba?\(([^)]+)\)/.exec(value);
    if (!match) return null;
    const parts = match[1].split(/[,\s/]+/).filter((part) => part.length > 0).map(Number);
    if (parts.length < 3 || parts.some((part) => Number.isNaN(part))) return null;
    return { r: parts[0], g: parts[1], b: parts[2], a: parts.length > 3 ? parts[3] : 1 };
  };
  const over = (front, back) => ({
    r: front.r * front.a + back.r * (1 - front.a),
    g: front.g * front.a + back.g * (1 - front.a),
    b: front.b * front.a + back.b * (1 - front.a),
    a: 1,
  });
  const lin = (value) => {
    const scaled = value / 255;
    return scaled <= 0.03928 ? scaled / 12.92 : ((scaled + 0.055) / 1.055) ** 2.4;
  };
  const lum = (colour) => 0.2126 * lin(colour.r) + 0.7152 * lin(colour.g) + 0.0722 * lin(colour.b);
  const ratio = (first, second) => {
    const a = lum(first);
    const b = lum(second);
    return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
  };
  const hex = (colour) =>
    "#" + [colour.r, colour.g, colour.b].map((value) => Math.round(value).toString(16).padStart(2, "0")).join("");

  const base = parse(getComputedStyle(document.body).backgroundColor) ?? { r: 255, g: 255, b: 255, a: 1 };
  const SHAPES = new Set(["path", "rect", "circle", "ellipse", "polygon", "polyline", "line"]);

  const ancestorBackdrop = (element) => {
    const chain = [];
    let node = element;
    while (node && node !== document.documentElement) {
      chain.push(node);
      node = node.parentElement;
    }
    let acc = base;
    for (const item of chain.reverse()) {
      const colour = parse(getComputedStyle(item).backgroundColor);
      if (colour && colour.a > 0) acc = over(colour, acc);
    }
    return acc;
  };

  const paintedBackdrop = (element, skip = null) => {
    const box = element.getBoundingClientRect();
    const x = Math.min(Math.max(box.left + box.width / 2, 0), window.innerWidth - 1);
    const y = Math.min(Math.max(box.top + box.height / 2, 0), window.innerHeight - 1);
    const stack = document.elementsFromPoint(x, y);
    let acc = base;
    for (const item of [...stack].reverse()) {
      if (item === skip) continue;
      const tag = item.tagName.toLowerCase();
      if (item instanceof SVGElement) {
        if (!SHAPES.has(tag)) continue;
        const fill = parse(getComputedStyle(item).fill);
        if (fill && fill.a > 0) acc = over(fill, acc);
      } else {
        const colour = parse(getComputedStyle(item).backgroundColor);
        if (colour && colour.a > 0) acc = over(colour, acc);
      }
    }
    return acc;
  };

  const backdrop = (element) => (element.closest("svg") ? paintedBackdrop(element, element) : ancestorBackdrop(element));

  const name = (element) => {
    const parts = [];
    let node = element;
    while (node && node !== document.documentElement && parts.length < 4) {
      let step = node.tagName.toLowerCase();
      if (node.id) step += "#" + node.id.replace(/mermaid-\d+-/, "");
      const classes = (node.getAttribute("class") ?? "").trim().split(/\s+/).filter(Boolean).slice(0, 2);
      if (classes.length) step += "." + classes.join(".");
      parts.unshift(step);
      node = node.parentElement;
    }
    return parts.join(">");
  };

  const measure = (element, text, colour) => {
    const behind = backdrop(element);
    const style = getComputedStyle(element);
    const size = Number.parseFloat(style.fontSize);
    const weight = Number(style.fontWeight) || 400;
    const large = size >= 24 || (size >= 18.66 && weight >= 700);
    const floor = large ? 3 : 4.5;
    const value = ratio(colour, behind);
    return {
      where: name(element),
      text: text.replace(/\s+/g, " ").trim().slice(0, 40),
      color: hex(colour),
      bg: hex(behind),
      ratio: Number(value.toFixed(3)),
      floor,
      size: Number(size.toFixed(1)),
      weight,
      pass: value >= floor,
    };
  };

  const rows = [];
  const seen = new Set();
  for (const element of document.querySelectorAll("body *")) {
    if (element.closest("script") || element.closest("style")) continue;
    const hasText = [...element.childNodes].some(
      (node) => node.nodeType === 3 && (node.textContent ?? "").trim().length > 0,
    );
    if (!hasText || seen.has(element)) continue;
    const style = getComputedStyle(element);
    if (style.visibility === "hidden" || style.display === "none") continue;
    const box = element.getBoundingClientRect();
    if (box.width < 1 || box.height < 1) continue;
    if (Number.parseFloat(style.opacity) === 0) continue;
    const colour = parse(style.color);
    if (!colour || colour.a === 0) continue;
    seen.add(element);
    rows.push(measure(element, element.textContent ?? "", colour));
  }

  // A closed select paints the selected option's text inside its own box. The
  // option itself has a 0x0 box, so it is measured through the select.
  for (const select of document.querySelectorAll("select")) {
    const option = select.selectedOptions[0];
    if (!option) continue;
    const style = getComputedStyle(select);
    if (style.visibility === "hidden" || style.display === "none") continue;
    const box = select.getBoundingClientRect();
    if (box.width < 1 || box.height < 1) continue;
    const colour = parse(style.color);
    if (!colour || colour.a === 0) continue;
    rows.push(measure(select, option.textContent ?? "", colour));
  }

  const content = document.querySelector("#panel-content");
  const counts = {};
  for (const selector of counters) {
    counts[selector] = content === null ? 0 : content.querySelectorAll(selector).length;
  }

  return {
    rows,
    labelCount: document.querySelectorAll(".mermaid p").length,
    counts,
    panelText: (content?.textContent ?? "").replace(/\s+/g, " ").trim(),
  };
}

function focusRing() {
  const parse = (value) => {
    const match = /rgba?\(([^)]+)\)/.exec(value ?? "");
    if (!match) return null;
    const parts = match[1].split(/[,\s/]+/).filter(Boolean).map(Number);
    return { r: parts[0], g: parts[1], b: parts[2], a: parts.length > 3 ? parts[3] : 1 };
  };
  const lin = (value) => {
    const scaled = value / 255;
    return scaled <= 0.03928 ? scaled / 12.92 : ((scaled + 0.055) / 1.055) ** 2.4;
  };
  const lum = (colour) => 0.2126 * lin(colour.r) + 0.7152 * lin(colour.g) + 0.0722 * lin(colour.b);
  const ratio = (first, second) => {
    const a = lum(first);
    const b = lum(second);
    return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
  };
  const hex = (colour) =>
    "#" + [colour.r, colour.g, colour.b].map((value) => Math.round(value).toString(16).padStart(2, "0")).join("");

  const element = document.activeElement;
  if (!element || element === document.body) return null;
  const style = getComputedStyle(element);
  const outline = parse(style.outlineColor);
  if (!outline) return null;
  // A ring that is not painted is not a ring. Skip an element whose outline
  // is absent or zero width so the walk cannot compare a colour nobody sees.
  const width = Number.parseFloat(style.outlineWidth);
  if (style.outlineStyle === "none" || !Number.isFinite(width) || width <= 0) return null;
  const box = element.getBoundingClientRect();
  const stack = document.elementsFromPoint(
    Math.min(Math.max(box.left - 2, 0), window.innerWidth - 1),
    Math.min(Math.max(box.top + box.height / 2, 0), window.innerHeight - 1),
  );
  const SHAPES = new Set(["path", "rect", "circle", "ellipse", "polygon", "polyline", "line"]);
  let acc = parse(getComputedStyle(document.body).backgroundColor) ?? { r: 255, g: 255, b: 255, a: 1 };
  for (const item of [...stack].reverse()) {
    const tag = item.tagName.toLowerCase();
    const colour = item instanceof SVGElement
      ? (SHAPES.has(tag) ? parse(getComputedStyle(item).fill) : null)
      : parse(getComputedStyle(item).backgroundColor);
    if (colour && colour.a > 0) {
      acc = {
        r: colour.r * colour.a + acc.r * (1 - colour.a),
        g: colour.g * colour.a + acc.g * (1 - colour.a),
        b: colour.b * colour.a + acc.b * (1 - colour.a),
        a: 1,
      };
    }
  }
  const target = element.id
    ? `#${element.id}`
    : `${element.tagName.toLowerCase()}${element.dataset["panel"] ? `[${element.dataset["panel"]}]` : ""}`;
  const value = ratio(outline, acc);
  return {
    index: Number(element.dataset["auditFocus"] ?? "-1"),
    target,
    outline: hex(outline),
    bg: hex(acc),
    ratio: Number(value.toFixed(3)),
    floor: 3,
    pass: value >= 3,
  };
}

function tagFocusables() {
  const selector = 'a[href], button, select:not([disabled]), [tabindex="0"]';
  const nodes = [...document.querySelectorAll(selector)].filter((node) => {
    const style = getComputedStyle(node);
    const box = node.getBoundingClientRect();
    return style.visibility !== "hidden" && style.display !== "none" && box.width >= 1 && box.height >= 1;
  });
  nodes.forEach((node, index) => node.setAttribute("data-audit-focus", String(index)));
  return nodes.length;
}

await mkdir(projectRoot, { recursive: true });
await mkdir(resolve(projectRoot, ".cmsc127"), { recursive: true });
await mkdir(artifactRoot, { recursive: true });
await writeFile(
  resolve(projectRoot, "README.md"),
  `\`\`\`mermaid\nerDiagram\n  ${TABLE} {\n    int id PK\n    text name\n  }\n\`\`\`\n`,
  "utf-8",
);
await writeFile(checkResultPath, JSON.stringify(CHECK_RESULT, null, 1), "utf-8");

await runSql([
  `CREATE TABLE IF NOT EXISTS public.${TABLE} (id integer PRIMARY KEY, name text NOT NULL)`,
  `TRUNCATE public.${TABLE}`,
  `INSERT INTO public.${TABLE} VALUES (1, 'Ana'), (2, 'Ben')`,
  `CREATE SCHEMA IF NOT EXISTS ${EMPTY_SCHEMA}`,
]);

const server = spawn(python, [resolve("server.py"), "--no-browser"], {
  cwd: projectRoot,
  env: { ...process.env, CMSC127_PORT: PORT },
  stdio: ["ignore", "pipe", "pipe"],
  windowsHide: true,
});

// The Sessions panel lists every backend except its own. Hold one connection
// open for the run so the panel always paints a session row, in every theme,
// instead of depending on whether the server happens to hold a second one.
const sessionHolder = spawn(python, ["-c", [
  "import os, time, psycopg",
  "with psycopg.connect(os.environ['CMSC127_DATABASE_URL']) as connection:",
  "    connection.execute('select 1')",
  "    time.sleep(3600)",
].join("\n")], { stdio: "ignore", env: process.env, windowsHide: true });

let serverOutput = "";
server.stdout.on("data", (chunk) => { serverOutput += chunk.toString(); });
server.stderr.on("data", (chunk) => { serverOutput += chunk.toString(); });

const report = {
  themes: {},
  textFailures: [],
  focusFailures: [],
  viewFailures: [],
  focus: {},
  labelCounts: [],
};
let browser;
let failure = null;

try {
  const deadline = Date.now() + 20_000;
  let address;
  while (Date.now() < deadline) {
    const match = /CMSC 127 Harness is available at (http:\/\/127\.0\.0\.1:\d+\/)/.exec(serverOutput);
    if (match?.[1]) {
      address = match[1];
      break;
    }
    if (server.exitCode !== null) break;
    await delay(50);
  }
  if (!address) throw new Error(`The audit server did not print its address. ${serverOutput}`);

  const sessionDeadline = Date.now() + 10_000;
  let sessionVisible = false;
  while (Date.now() < sessionDeadline) {
    try {
      const response = await fetch(`${address}api/sessions`);
      if (response.ok) {
        const data = await response.json();
        if (Array.isArray(data.sessions) && data.sessions.length > 0) {
          sessionVisible = true;
          break;
        }
      }
    } catch {
      // The server may not answer on the first try.
    }
    await delay(100);
  }
  if (!sessionVisible) throw new Error("The audit's own session never appeared in the Sessions panel.");

  browser = await chromium.launch({ headless: true });

  for (const theme of THEMES) {
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    await page.goto(address, { waitUntil: "domcontentloaded" });
    await page.evaluate((value) => localStorage.setItem("cmsc127-theme", value), theme);
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.waitForFunction(
      () => document.querySelectorAll("#panels button").length === 7,
      undefined,
      { timeout: 15_000 },
    );
    await page.waitForFunction(
      () => document.querySelectorAll("#panel-content tbody tr").length > 0,
      undefined,
      { timeout: 15_000 },
    );

    report.themes[theme] = {};

    const measureView = async (view) => {
      const result = await page.evaluate(auditPage, COUNTERS);
      report.themes[theme][view.name] = result.rows.length;
      if (!report.labelCounts.includes(result.labelCount)) report.labelCounts.push(result.labelCount);
      for (const row of result.rows.filter((entry) => !entry.pass)) {
        report.textFailures.push({ theme, view: view.name, ...row });
      }
      const problems = [];
      if (result.rows.length < view.min) {
        problems.push(`measured ${result.rows.length} text elements, expected at least ${view.min}`);
      }
      for (const [selector, count] of Object.entries(view.needs ?? {})) {
        const actual = result.counts[selector] ?? 0;
        if (actual < count) problems.push(`${selector}: ${actual}, expected at least ${count}`);
      }
      for (const text of view.text ?? []) {
        if (!result.panelText.includes(text)) problems.push(`panel text does not include ${JSON.stringify(text)}`);
      }
      if (problems.length > 0) report.viewFailures.push({ theme, view: view.name, problems });
    };

    // The initial view, where a scroll container is also focusable.
    await measureView(VIEWS[0]);

    const focusables = await page.evaluate(tagFocusables);
    const seen = new Set();
    await page.locator("body").click({ position: { x: 5, y: 5 } });
    for (let step = 0; step < focusables + 4; step += 1) {
      await page.keyboard.press("Tab");
      await page.waitForTimeout(60);
      const ring = await page.evaluate(focusRing);
      if (!ring) continue;
      seen.add(ring.index);
      if (!ring.pass) report.focusFailures.push({ theme, ...ring });
    }
    const missing = [...Array(focusables).keys()].filter((index) => !seen.has(index));
    report.focus[theme] = { focusables, measured: seen.size, missing };

    for (const view of VIEWS.slice(1)) {
      if (view.schema) {
        await page.selectOption("#schema-select", view.schema);
      }
      if (view.route) {
        await page.route("**/api/status*", (route) => route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            databaseStatus: "disconnected",
            server: "running",
            message: "PostgreSQL is unavailable. Check the database server and connection settings.",
          }),
        }));
      }
      if (view.hideCheckResult) {
        await unlink(checkResultPath).catch(() => undefined);
      }
      if (view.panel) {
        await page.getByRole("button", { name: view.panel, exact: true }).click();
      }
      try {
        await page.waitForFunction(
          (expected) => {
            const content = document.querySelector("#panel-content");
            if (content === null) return false;
            const text = (content.textContent ?? "").replace(/\s+/g, " ").trim();
            if (text.includes("Loading…")) return false;
            if (expected.text && !text.includes(expected.text)) return false;
            if (expected.any.length > 0 && !expected.any.some((item) => text.includes(item))) return false;
            if (expected.selector && content.querySelector(expected.selector) === null) return false;
            return true;
          },
          { text: view.waitText ?? "", any: view.waitAny ?? [], selector: view.waitSelector ?? "" },
          { timeout: 15_000 },
        );
      } catch {
        throw new Error(
          `The ${view.name} view did not reach its expected state [${theme}]. ` +
            `Panel text: ${JSON.stringify((await page.evaluate(() => document.querySelector("#panel-content")?.textContent ?? "")).slice(0, 200))}`,
        );
      }
      if (view.name === "Schema") {
        await page.waitForFunction(
          () => {
            const nodes = [...document.querySelectorAll(".mermaid")];
            return nodes.length === 2 && nodes.every((node) => node.querySelector("svg") !== null);
          },
          undefined,
          { timeout: 15_000 },
        );
      }
      await page.waitForTimeout(150);
      await measureView(view);
      if (view.route) await page.unroute("**/api/status*");
      if (view.hideCheckResult) {
        await writeFile(checkResultPath, JSON.stringify(CHECK_RESULT, null, 1), "utf-8");
      }
    }

    await page.close();
  }
} catch (error) {
  failure = error instanceof Error ? error : new Error(String(error));
} finally {
  if (browser) await browser.close();
  if (server.exitCode === null) {
    const stopped = new Promise((ready) => server.once("exit", ready));
    server.kill();
    await stopped;
  }
  if (sessionHolder.exitCode === null) {
    const stopped = new Promise((ready) => sessionHolder.once("exit", ready));
    sessionHolder.kill();
    await stopped;
  }
  await runSql([`DROP TABLE IF EXISTS public.${TABLE}`, `DROP SCHEMA IF EXISTS ${EMPTY_SCHEMA} CASCADE`]);
  await unlink(checkResultPath).catch(() => undefined);
}

const measured = Object.values(report.themes).reduce(
  (total, views) => total + Object.values(views).reduce((sum, count) => sum + count, 0),
  0,
);

await writeFile(resolve(artifactRoot, "contrast.json"), JSON.stringify(report, null, 1), "utf-8");

const range = (view) => {
  const counts = Object.values(report.themes).map((views) => views[view]).filter((count) => count !== undefined);
  return counts.length === 0 ? "0" : `${Math.min(...counts)}-${Math.max(...counts)}`;
};
const rings = Object.values(report.focus).map((entry) => entry.measured);

console.log(`themes: ${THEMES.length}, views: ${VIEWS.length}`);
console.log(`text elements measured: ${measured}`);
console.log(`per view: ${VIEWS.map((view) => `${view.name} ${range(view.name)}`).join(", ")}`);
console.log(`diagram label counts seen: ${report.labelCounts.join(", ")}`);
console.log(`focusable elements: ${[...new Set(Object.values(report.focus).map((entry) => entry.focusables))].join(", ")}`);
console.log(`focus rings per theme: ${rings.length === 0 ? "0" : `${Math.min(...rings)}-${Math.max(...rings)}`}`);
console.log(`text failures: ${report.textFailures.length}`);
for (const item of report.textFailures.slice(0, 40)) {
  console.log(
    `  ${item.ratio} < ${item.floor}  ${item.color} on ${item.bg}  ${item.size}px/${item.weight}  ` +
      `"${item.text}"  ${item.where}  [${item.theme} ${item.view}]`,
  );
}
console.log(`focus ring failures: ${report.focusFailures.length}`);
for (const item of report.focusFailures) {
  console.log(`  ${item.ratio} < ${item.floor}  ${item.outline} on ${item.bg}  ${item.target}  [${item.theme}]`);
}
console.log(`view failures: ${report.viewFailures.length}`);
for (const item of report.viewFailures.slice(0, 20)) {
  console.log(`  ${item.view}  [${item.theme}]  ${item.problems.join("; ")}`);
}
for (const [theme, entry] of Object.entries(report.focus)) {
  if (entry.missing.length > 0) {
    console.log(`  ${theme}: ${entry.measured}/${entry.focusables} focusables painted a ring, missing ${entry.missing.join(", ")}`);
  }
}

if (failure) {
  console.error(`The contrast audit could not finish. ${failure.message}`);
  process.exitCode = 1;
} else if (report.textFailures.length > 0) {
  console.error("The contrast audit found a text pair below its floor.");
  process.exitCode = 1;
} else if (report.focusFailures.length > 0) {
  console.error("The contrast audit found a focus ring below 3:1.");
  process.exitCode = 1;
} else if (report.viewFailures.length > 0) {
  console.error("A view did not render what it must contain.");
  process.exitCode = 1;
} else if (Object.values(report.focus).some((entry) => entry.missing.length > 0 || entry.measured === 0)) {
  console.error("A theme did not paint a ring on every focusable element.");
  process.exitCode = 1;
} else if (measured === 0) {
  console.error("The contrast audit measured no text. The page did not render.");
  process.exitCode = 1;
} else {
  console.log("Contrast audit passed.");
}
