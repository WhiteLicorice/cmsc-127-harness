// Rendered-page contrast audit.
//
// The CSS test reads tokens. This script reads the page. It walks every element
// that paints text, in every theme and every panel, and compares the computed
// colour with the backdrop really painted behind it. HTML backdrops come from
// the ancestor background chain. Anything inside an SVG takes its backdrop from
// the painted shapes under the point, because a <g> paints nothing.
//
// It also walks the focus order and checks each focus ring against the surface
// behind it. Text must reach 4.5:1, or 3:1 when it is large. A focus ring must
// reach 3:1.
import { spawn } from "node:child_process";
import { mkdir, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { chromium } from "playwright";

const THEMES = [
  "upv-light", "upv-dark", "github-light", "github-dark", "one-dark", "nord",
  "solarized-light", "solarized-dark", "catppuccin-latte", "catppuccin-mocha",
  "tokyo-night", "gruvbox-light", "gruvbox-dark",
];
const PANELS = ["Tables", "Changes", "Schema", "Sessions", "Checks", "Roles", "Status"];
const PORT = "55447";
const TABLE = "audit_student";
const projectRoot = resolve("audit-project");
const artifactRoot = resolve("audit-artifacts");
const python = resolve(process.platform === "win32" ? ".venv\\Scripts\\python.exe" : ".venv/bin/python");

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

function auditPage() {
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
    const behind = backdrop(element);
    const size = Number.parseFloat(style.fontSize);
    const weight = Number(style.fontWeight) || 400;
    const large = size >= 24 || (size >= 18.66 && weight >= 700);
    const floor = large ? 3 : 4.5;
    const value = ratio(colour, behind);
    seen.add(element);
    rows.push({
      where: name(element),
      text: (element.textContent ?? "").replace(/\s+/g, " ").trim().slice(0, 40),
      color: hex(colour),
      bg: hex(behind),
      ratio: Number(value.toFixed(3)),
      floor,
      size: Number(size.toFixed(1)),
      weight,
      pass: value >= floor,
    });
  }

  const labelCount = document.querySelectorAll(".mermaid p").length;
  return { rows, labelCount };
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
  return { target, outline: hex(outline), bg: hex(acc), ratio: Number(value.toFixed(3)), floor: 3, pass: value >= 3 };
}

await mkdir(projectRoot, { recursive: true });
await mkdir(artifactRoot, { recursive: true });
await writeFile(
  resolve(projectRoot, "README.md"),
  `\`\`\`mermaid\nerDiagram\n  ${TABLE} {\n    int id PK\n    text name\n  }\n\`\`\`\n`,
  "utf-8",
);

await runSql([
  `CREATE TABLE IF NOT EXISTS public.${TABLE} (id integer PRIMARY KEY, name text NOT NULL)`,
  `TRUNCATE public.${TABLE}`,
  `INSERT INTO public.${TABLE} VALUES (1, 'Ana'), (2, 'Ben')`,
]);

const server = spawn(python, [resolve("server.py"), "--no-browser"], {
  cwd: projectRoot,
  env: { ...process.env, CMSC127_PORT: PORT },
  stdio: ["ignore", "pipe", "pipe"],
  windowsHide: true,
});

let serverOutput = "";
server.stdout.on("data", (chunk) => { serverOutput += chunk.toString(); });
server.stderr.on("data", (chunk) => { serverOutput += chunk.toString(); });

const report = { themes: {}, textFailures: [], focusFailures: [], labelCounts: [] };
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
    const views = ["initial", ...PANELS];
    for (const view of views) {
      if (view !== "initial") {
        await page.getByRole("button", { name: view, exact: true }).click();
        await page.waitForTimeout(view === "Schema" ? 1800 : 500);
        if (view === "Schema") {
          await page.waitForFunction(
            () => {
              const nodes = [...document.querySelectorAll(".mermaid")];
              return nodes.length === 2 && nodes.every((node) => node.querySelector("svg") !== null);
            },
            undefined,
            { timeout: 15_000 },
          );
          await page.waitForTimeout(500);
        }
      }
      const result = await page.evaluate(auditPage);
      report.themes[theme][view] = result.rows.length;
      if (!report.labelCounts.includes(result.labelCount)) report.labelCounts.push(result.labelCount);
      for (const row of result.rows.filter((entry) => !entry.pass)) {
        report.textFailures.push({ theme, view, ...row });
      }
    }

    await page.locator("body").click({ position: { x: 5, y: 5 } });
    for (let step = 0; step < 14; step += 1) {
      await page.keyboard.press("Tab");
      await page.waitForTimeout(60);
      const ring = await page.evaluate(focusRing);
      if (ring && !ring.pass) report.focusFailures.push({ theme, ...ring });
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
  await runSql([`DROP TABLE IF EXISTS public.${TABLE}`]);
}

const measured = Object.values(report.themes).reduce(
  (total, views) => total + Object.values(views).reduce((sum, count) => sum + count, 0),
  0,
);

await writeFile(resolve(artifactRoot, "contrast.json"), JSON.stringify(report, null, 1), "utf-8");

console.log(`themes: ${THEMES.length}, views: ${1 + PANELS.length}`);
console.log(`text elements measured: ${measured}`);
console.log(`diagram label counts seen: ${report.labelCounts.join(", ")}`);
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

if (failure) {
  console.error(`The contrast audit could not finish. ${failure.message}`);
  process.exitCode = 1;
} else if (report.textFailures.length > 0 || report.focusFailures.length > 0) {
  console.error("The contrast audit found a pair below its floor.");
  process.exitCode = 1;
} else if (measured === 0) {
  console.error("The contrast audit measured no text. The page did not render.");
  process.exitCode = 1;
} else {
  console.log("Contrast audit passed.");
}
