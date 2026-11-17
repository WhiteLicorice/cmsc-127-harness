// Rendered-page contrast audit.
//
// The CSS test reads tokens. This script reads the page. It walks every element
// that paints text, in every theme and every view, and compares the computed
// colour with the backdrop really painted behind it. HTML backdrops come from
// the ancestor background chain. Anything inside an SVG takes its backdrop from
// the painted shapes under the point, because a <g> paints nothing.
//
// Visible means the browser says so. One rule, VISIBILITY_RULE below, is used
// by the text walk, the select loop, the required selectors, the required text,
// the connection message, and the tabbable set. An element is visible when
// checkVisibility() is true and a hit test inside its clipped rectangle returns
// the element or a descendant. Clipping intersects the viewport and every
// ancestor whose overflow is not visible, so a 1px scroll container hides its
// rows, and a covered element is not visible either.
//
// A closed <select> paints its selected option's text but the option has a 0x0
// box, so the walk measures the select itself: its own box, colour, and
// background, with the selected option's text. The open list is a native popup
// outside the DOM and is not measured.
//
// Every view asserts what it must contain, and every assertion reads the page.
// Counts the fixture fixes are exact: rows, headers, result cells, table cards.
// A count of 0 means exactly zero. Only Sessions and Roles keep a lower bound,
// because their row counts follow the cluster.
//
// Each view names the panel it measures. The panel id must match and the panel
// must contain a marker only that panel renders, so a view cannot measure a
// different panel's markup. A view that clicks a tab must see the panel change.
//
// The chrome baseline is 14 measured elements: the title, the byline, the two
// control labels, the poll summary, the seven tabs, and the two closed select
// values. The lower bounds for Sessions and Roles are that baseline plus the
// fixture's row.
//
// The tab order comes from the browser: the walk presses Tab and records
// document.activeElement until focus cycles. An independent set of visible
// tabbables comes from a broad selector. Every member of that set must be
// visited, so a focus trap fails instead of shrinking the walk.
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
const VIEWPORT = { width: 1440, height: 900 };
const TABLE = "audit_student";
const EMPTY_SCHEMA = "audit_empty";
const CHROME_BASELINE = 14;
const projectRoot = resolve("audit-project");
const artifactRoot = resolve("audit-artifacts");
const checkResultPath = resolve(projectRoot, ".cmsc127", "last-run.json");
const python = resolve(process.platform === "win32" ? ".venv\\Scripts\\python.exe" : ".venv/bin/python");

// The one visibility rule. It is injected into every page function so the walk,
// the assertions, and the tabbable set cannot drift apart.
//
// Content the user can scroll to is painted: a container whose overflow is auto
// or scroll and whose content overflows it does not clip the hit test. Content
// below the fold is handled by sizing the viewport to the document before each
// view.
//
// A cover is not a yes-or-no test. coverLayers() returns the layers painted
// above the text, and measure() composites them over both the text colour and
// the backdrop, so the contrast ratio falls in proportion to what the cover
// paints. A 2% tint barely moves it; a 90% scrim drops it to about 1.16; an
// opaque layer makes the two colours equal and the ratio 1.
//
// A pseudo-element is a cover only where it paints: its rectangle is derived
// from the originating element's border box and the pseudo's computed offsets
// and size, and the layer counts only when that rectangle contains the sampled
// point. A gradient contributes its highest-alpha colour stop; a url() image is
// opaque, which is a named limit.
const VISIBILITY_RULE = `
  const parse = (value) => {
    if (!value || value === "none" || value === "transparent") return null;
    const match = /rgba?\\(([^)]+)\\)/.exec(value);
    if (!match) return null;
    const parts = match[1].split(/[,\\s/]+/).filter((part) => part.length > 0).map(Number);
    if (parts.length < 3 || parts.some((part) => Number.isNaN(part))) return null;
    return { r: parts[0], g: parts[1], b: parts[2], a: parts.length > 3 ? parts[3] : 1 };
  };
  // Resolve any computed colour to sRGB. rgb()/rgba() are parsed directly; the
  // other syntaxes (oklch, oklab, lab, lch, color, color-mix) go through a 1x1
  // canvas, so a gradient stop is measured by its real alpha rather than being
  // skipped.
  const colourCache = new Map();
  const resolveColour = (value) => {
    if (!value || value === "none" || value === "transparent") return null;
    const cached = colourCache.get(value);
    if (cached !== undefined) return cached;
    const parsed = parse(value);
    if (parsed !== null) {
      colourCache.set(value, parsed);
      return parsed;
    }
    const canvas = document.createElement("canvas");
    canvas.width = 1;
    canvas.height = 1;
    const context = canvas.getContext("2d", { willReadFrequently: true });
    if (context === null) {
      colourCache.set(value, null);
      return null;
    }
    context.clearRect(0, 0, 1, 1);
    context.fillStyle = "#010203";
    context.fillStyle = value;
    context.fillRect(0, 0, 1, 1);
    const data = context.getImageData(0, 0, 1, 1).data;
    const colour = data[3] === 0 ? null : { r: data[0], g: data[1], b: data[2], a: data[3] / 255 };
    colourCache.set(value, colour);
    return colour;
  };
  const SHAPES = new Set(["path", "rect", "circle", "ellipse", "polygon", "polyline", "line"]);
  const visibleCache = new Map();
  const clipCache = new Map();
  const coverCache = new Map();
  const pseudoCache = new Map();
  const scrollableAncestor = (element) => {
    for (let node = element.parentElement; node && node !== document.documentElement; node = node.parentElement) {
      const style = getComputedStyle(node);
      const scrollsX = (style.overflowX === "auto" || style.overflowX === "scroll")
        && node.scrollWidth > node.clientWidth;
      const scrollsY = (style.overflowY === "auto" || style.overflowY === "scroll")
        && node.scrollHeight > node.clientHeight;
      if (scrollsX || scrollsY) return node;
    }
    return null;
  };
  // What one painted layer contributes: its background image over its
  // background colour, times its opacity. A gradient whose stops all agree
  // contributes that colour; a gradient whose stops differ is a warning, not a
  // cover, and the text is measured without it. A url() image is opaque.
  const px = (value) => {
    if (!value || value === "auto") return null;
    const number = Number.parseFloat(value);
    return Number.isFinite(number) ? number : null;
  };
  const imageLayer = (value) => {
    if (!value || value === "none") return null;
    if (value.includes("url(")) {
      return { colour: { r: 0, g: 0, b: 0, a: 1 }, gradient: value, varying: false, highest: null };
    }
    const stops = [];
    const pattern = /(rgba?|oklch|oklab|lab|lch|color|color-mix)\\(([^)]*)\\)/g;
    let match = pattern.exec(value);
    while (match !== null) {
      const colour = resolveColour(match[0]);
      if (colour !== null) stops.push(colour);
      match = pattern.exec(value);
    }
    if (stops.length === 0) return null;
    const first = stops[0];
    const same = stops.every((stop) => Math.round(stop.r) === Math.round(first.r)
      && Math.round(stop.g) === Math.round(first.g)
      && Math.round(stop.b) === Math.round(first.b)
      && Math.abs(stop.a - first.a) < 0.002);
    const highest = stops.reduce((best, stop) => (stop.a > best.a ? stop : best));
    return { colour: same ? first : null, gradient: value, varying: !same, highest };
  };
  const layerColour = (style) => {
    const image = imageLayer(style.backgroundImage);
    const colour = resolveColour(style.backgroundColor);
    let base = null;
    let warning = null;
    if (image !== null && image.varying) {
      warning = { gradient: image.gradient, highest: image.highest };
      base = colour;
    } else if (image !== null && image.colour !== null) {
      base = colour === null
        ? image.colour
        : {
            r: image.colour.r * image.colour.a + colour.r * (1 - image.colour.a),
            g: image.colour.g * image.colour.a + colour.g * (1 - image.colour.a),
            b: image.colour.b * image.colour.a + colour.b * (1 - image.colour.a),
            a: image.colour.a + colour.a * (1 - image.colour.a),
          };
    } else {
      base = colour;
    }
    if (base === null) return { layer: null, warning };
    const opacity = Number.parseFloat(style.opacity);
    const alpha = base.a * (Number.isFinite(opacity) ? opacity : 1);
    if (alpha <= 0) return { layer: null, warning };
    return { layer: { r: base.r, g: base.g, b: base.b, a: Math.min(1, alpha) }, warning };
  };
  // Where a pseudo-element paints, from its originating element's border box,
  // the pseudo's computed offsets and size, and its padding and border.
  const pseudoRect = (node, style) => {
    const box = node.getBoundingClientRect();
    const top = px(style.top);
    const right = px(style.right);
    const bottom = px(style.bottom);
    const left = px(style.left);
    const width = px(style.width);
    const height = px(style.height);
    const padX = (px(style.paddingLeft) ?? 0) + (px(style.paddingRight) ?? 0)
      + (px(style.borderLeftWidth) ?? 0) + (px(style.borderRightWidth) ?? 0);
    const padY = (px(style.paddingTop) ?? 0) + (px(style.paddingBottom) ?? 0)
      + (px(style.borderTopWidth) ?? 0) + (px(style.borderBottomWidth) ?? 0);
    const borderBox = style.boxSizing === "border-box";
    const w = width !== null
      ? (borderBox ? width : width + padX)
      : left !== null && right !== null ? box.width - left - right : box.width;
    const h = height !== null
      ? (borderBox ? height : height + padY)
      : top !== null && bottom !== null ? box.height - top - bottom : box.height;
    const x = left !== null ? box.left + left : right !== null ? box.right - right - w : box.left;
    const y = top !== null ? box.top + top : bottom !== null ? box.bottom - bottom - h : box.top;
    return { left: x, top: y, right: x + w, bottom: y + h, width: w, height: h };
  };
  const lengthValue = (value, basis) => {
    if (!value) return 0;
    if (value.endsWith("%")) return basis * (Number.parseFloat(value) / 100);
    const number = Number.parseFloat(value);
    return Number.isFinite(number) ? number : 0;
  };
  const insideQuad = (point, quad) => {
    let inside = false;
    for (let index = 0, previous = quad.length - 1; index < quad.length; previous = index, index += 1) {
      const current = quad[index];
      const last = quad[previous];
      if ((current.y > point.y) !== (last.y > point.y)
        && point.x < (last.x - current.x) * (point.y - current.y) / (last.y - current.y) + current.x) {
        inside = !inside;
      }
    }
    return inside;
  };
  const pseudoQuad = (rect, style) => {
    if (!style.transform || style.transform === "none") return null;
    const matrix = new DOMMatrix(style.transform);
    const parts = style.transformOrigin.split(/\\s+/);
    const originX = rect.left + lengthValue(parts[0], rect.width);
    const originY = rect.top + lengthValue(parts[1], rect.height);
    const full = new DOMMatrix().translate(originX, originY).multiply(matrix).translate(-originX, -originY);
    return [
      { x: rect.left, y: rect.top },
      { x: rect.right, y: rect.top },
      { x: rect.right, y: rect.bottom },
      { x: rect.left, y: rect.bottom },
    ].map((corner) => {
      const mapped = full.transformPoint(new DOMPoint(corner.x, corner.y));
      return { x: mapped.x, y: mapped.y };
    });
  };
  // A pseudo-element paints above its originating element's content when it has
  // content, is positioned, is not behind it, and its mapped rectangle contains
  // the sampled point. An inset:0 overlay covers; an underline, a corner dot, a
  // scale(0) overlay, and a translated-away overlay do not.
  const pseudoLayers = (node, point) => {
    const cached = pseudoCache.get(node);
    const descriptors = cached ?? [];
    if (cached === undefined) {
      for (const pseudo of ["::before", "::after"]) {
        const style = getComputedStyle(node, pseudo);
        if (style.content === "none" || style.content === "normal") continue;
        if (style.display === "none") continue;
        if (style.position === "static") continue;
        if (style.zIndex !== "auto" && Number(style.zIndex) < 0) continue;
        const { layer, warning } = layerColour(style);
        if (layer !== null || warning !== null) {
          const rect = pseudoRect(node, style);
          descriptors.push({ rect, quad: pseudoQuad(rect, style), layer, warning });
        }
      }
      pseudoCache.set(node, descriptors);
    }
    const layers = [];
    const warnings = [];
    for (const descriptor of descriptors) {
      const { rect, quad, layer, warning } = descriptor;
      const contains = quad === null
        ? point.x >= rect.left && point.x <= rect.right && point.y >= rect.top && point.y <= rect.bottom
        : insideQuad(point, quad);
      if (!contains) continue;
      if (layer !== null) layers.push(layer);
      if (warning !== null) warnings.push(warning);
    }
    return { layers, warnings };
  };
  const hitState = (element, box) => {
    const stack = document.elementsFromPoint((box.left + box.right) / 2, (box.top + box.bottom) / 2);
    return stack.some((item) => item === element || element.contains(item)) ? "visible" : "missing";
  };
  const clippedBox = (element) => {
    const cached = clipCache.get(element);
    if (cached !== undefined) return cached;
    const box = element.getBoundingClientRect();
    let left = 0;
    let top = 0;
    let right = window.innerWidth;
    let bottom = window.innerHeight;
    for (let node = element; node && node !== document.documentElement; node = node.parentElement) {
      if (node === element) continue;
      const style = getComputedStyle(node);
      const scrollsX = (style.overflowX === "auto" || style.overflowX === "scroll")
        && node.scrollWidth > node.clientWidth;
      const scrollsY = (style.overflowY === "auto" || style.overflowY === "scroll")
        && node.scrollHeight > node.clientHeight;
      const clipsX = style.overflowX !== "visible" && !scrollsX;
      const clipsY = style.overflowY !== "visible" && !scrollsY;
      if (clipsX || clipsY) {
        const rect = node.getBoundingClientRect();
        if (clipsX) {
          left = Math.max(left, rect.left + node.clientLeft);
          right = Math.min(right, rect.left + node.clientLeft + node.clientWidth);
        }
        if (clipsY) {
          top = Math.max(top, rect.top + node.clientTop);
          bottom = Math.min(bottom, rect.top + node.clientTop + node.clientHeight);
        }
      }
    }
    const clipped = {
      left: Math.max(left, box.left),
      top: Math.max(top, box.top),
      right: Math.min(right, box.right),
      bottom: Math.min(bottom, box.bottom),
    };
    clipCache.set(element, clipped);
    return clipped;
  };
  const isVisible = (element) => {
    if (!(element instanceof Element)) return false;
    const cached = visibleCache.get(element);
    if (cached !== undefined) return cached;
    let visible = true;
    if (!element.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true, contentVisibilityAuto: true })) {
      visible = false;
    } else {
      const box = element.getBoundingClientRect();
      if (box.width < 1 || box.height < 1) {
        visible = false;
      } else {
        let clipped = clippedBox(element);
        let state = clipped.right - clipped.left >= 1 && clipped.bottom - clipped.top >= 1
          ? hitState(element, clipped)
          : "missing";
        // Content inside a scrollport is painted once the user scrolls to it.
        // Scroll it into view, retry the hit test, then restore the scroll.
        if (state === "missing") {
          const scroller = scrollableAncestor(element);
          if (scroller !== null) {
            const savedTop = scroller.scrollTop;
            const savedLeft = scroller.scrollLeft;
            element.scrollIntoView({ block: "nearest", inline: "nearest" });
            clipCache.delete(element);
            clipped = clippedBox(element);
            if (clipped.right - clipped.left >= 1 && clipped.bottom - clipped.top >= 1) {
              state = hitState(element, clipped);
            }
            scroller.scrollTop = savedTop;
            scroller.scrollLeft = savedLeft;
          }
        }
        visible = state === "visible";
      }
    }
    visibleCache.set(element, visible);
    return visible;
  };
  // The layers painted above the text, bottom to top: non-ancestor items the
  // hit stack returns above it, then the positioned pseudo-elements of the text
  // and of each ancestor.
  const coverLayers = (element) => {
    const cached = coverCache.get(element);
    if (cached !== undefined) return cached;
    const layers = [];
    const warnings = [];
    const clipped = clippedBox(element);
    const box = element.getBoundingClientRect();
    const sampled = clipped.right - clipped.left >= 1 && clipped.bottom - clipped.top >= 1;
    const point = sampled
      ? { x: (clipped.left + clipped.right) / 2, y: (clipped.top + clipped.bottom) / 2 }
      : { x: (box.left + box.right) / 2, y: (box.top + box.bottom) / 2 };
    if (sampled) {
      const stack = document.elementsFromPoint(point.x, point.y);
      const textIndex = stack.findIndex((item) => item === element || element.contains(item));
      if (textIndex > 0) {
        for (let index = textIndex - 1; index >= 0; index -= 1) {
          const item = stack[index];
          if (item === element || item.contains(element) || element.contains(item)) continue;
          const { layer, warning } = layerColour(getComputedStyle(item));
          if (layer !== null) layers.push(layer);
          if (warning !== null) warnings.push(warning);
        }
      }
    }
    for (let node = element; node && node !== document.documentElement; node = node.parentElement) {
      const pseudo = pseudoLayers(node, point);
      layers.push(...pseudo.layers);
      warnings.push(...pseudo.warnings);
    }
    const result = { layers, warnings };
    coverCache.set(element, result);
    return result;
  };
  const paintsText = (element) => {
    const colour = resolveColour(getComputedStyle(element).color);
    return colour === null || colour.a > 0;
  };
`;

const TABBABLE_SELECTOR = [
  "a[href]", "button", "select", "input", "textarea", "summary",
  "[tabindex]", "[contenteditable='true']", "audio[controls]", "video[controls]",
].join(",");

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

// `exact` counts the fixture-fixed parts of a view; a count of 0 means exactly
// zero. `min` and `needs` are lower bounds only for Sessions and Roles, whose
// row counts follow the cluster.
const VIEWS = [
  {
    name: "initial",
    panelId: "tables",
    waitSelector: "tbody tr",
    exact: { "tbody tr": 2, "thead th": 3, ".table-card": 1 },
    text: ["Rows use committed data", "audit_student"],
  },
  {
    name: "Changes",
    panel: "Changes",
    panelId: "changes",
    waitText: "No changes observed yet",
    exact: { "tbody tr": 0 },
    text: ["No changes observed yet"],
  },
  {
    name: "Schema",
    panel: "Schema",
    panelId: "schema",
    waitSelector: ".mermaid svg",
    exact: { ".mermaid svg": 2, ".mermaid p": 12 },
    text: ["audit_student"],
  },
  {
    name: "Sessions",
    panel: "Sessions",
    panelId: "sessions",
    min: 26,
    waitSelector: "tbody tr",
    needs: { "tbody tr": 1 },
    exact: { "thead th": 6 },
    text: ["PostgreSQL limits which sessions a role can see.", "PID"],
  },
  {
    name: "Checks",
    panel: "Checks",
    panelId: "checks",
    waitSelector: ".result-pass",
    exact: {
      "tbody tr": 4, ".result-pass": 2, ".result-fail": 1, ".result-skip": 1, ".warning-note": 1,
    },
    text: ["Case", "Result", "3 rows exceed the soft limit"],
  },
  {
    name: "Roles",
    panel: "Roles",
    panelId: "roles",
    min: 20,
    waitSelector: "tbody tr",
    needs: { "tbody tr": 1 },
    exact: { "thead th": 3 },
    text: ["Privileges"],
  },
  {
    name: "Status",
    panel: "Status",
    panelId: "status",
    waitSelector: "dl dt",
    exact: { "dl dt": 4 },
    text: ["Last poll"],
  },
  {
    name: "Status disconnected",
    panel: "Status",
    panelId: "status",
    route: "status-disconnected",
    waitSelector: ".error-message",
    exact: { ".error-message": 1, "tbody tr": 0 },
    text: ["PostgreSQL is unavailable"],
  },
  {
    name: "Checks empty",
    panel: "Checks",
    panelId: "checks",
    hideCheckResult: true,
    waitText: "No check result exists",
    exact: { "tbody tr": 0 },
    text: ["No check result exists"],
  },
  {
    name: "Server unreachable",
    panel: "Tables",
    panelId: "tables",
    route: "tables-abort",
    waitConnection: "The harness server cannot be reached.",
    exact: { "tbody tr": 2, ".table-card": 1 },
    text: ["Rows use committed data"],
  },
  {
    name: "Tables error",
    panelId: "tables",
    route: "tables-error",
    waitConnection: "The tables request failed.",
    exact: { "tbody tr": 2, ".table-card": 1 },
    text: ["Rows use committed data"],
  },
  {
    name: "Tables empty",
    panel: "Tables",
    panelId: "tables",
    schema: EMPTY_SCHEMA,
    waitText: "No tables or views exist in this schema.",
    exact: { "tbody tr": 0, ".table-card": 0 },
    text: ["No tables or views exist in this schema."],
  },
  {
    name: "Roles empty",
    panel: "Roles",
    panelId: "roles",
    waitText: "No tables or user roles exist in this schema.",
    exact: { "tbody tr": 0 },
    text: ["No tables or user roles exist in this schema."],
  },
];

const COUNTERS = [
  "tbody tr", "thead th", "p", "dl dt", ".result-pass", ".result-fail", ".result-skip",
  ".warning-note", ".error-message", ".mermaid svg", ".mermaid p", ".table-card",
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

const auditPage = new Function("counters", `${VISIBILITY_RULE}
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

  const base = resolveColour(getComputedStyle(document.body).backgroundColor) ?? { r: 255, g: 255, b: 255, a: 1 };

  const ancestorBackdrop = (element) => {
    const chain = [];
    let node = element;
    while (node && node !== document.documentElement) {
      chain.push(node);
      node = node.parentElement;
    }
    let acc = base;
    for (const item of chain.reverse()) {
      const colour = resolveColour(getComputedStyle(item).backgroundColor);
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
        const fill = resolveColour(getComputedStyle(item).fill);
        if (fill && fill.a > 0) acc = over(fill, acc);
      } else {
        const colour = resolveColour(getComputedStyle(item).backgroundColor);
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
      if (node.id) step += "#" + node.id.replace(/mermaid-\\d+-/, "");
      const classes = (node.getAttribute("class") ?? "").trim().split(/\\s+/).filter(Boolean).slice(0, 2);
      if (classes.length) step += "." + classes.join(".");
      parts.unshift(step);
      node = node.parentElement;
    }
    return parts.join(">");
  };

  const measure = (element, text, colour) => {
    const behind = backdrop(element);
    // Composite the layers painted above the text over both the text and the
    // backdrop. An opaque cover makes them equal and the ratio 1; a tint moves
    // both slightly and leaves the ratio alone. A varying gradient is not
    // composited; it is reported as a warning at its highest-alpha stop.
    const cover = coverLayers(element);
    const paintedText = cover.layers.reduce((acc, layer) => over(layer, acc), colour);
    const paintedBehind = cover.layers.reduce((acc, layer) => over(layer, acc), behind);
    const style = getComputedStyle(element);
    const size = Number.parseFloat(style.fontSize);
    const weight = Number(style.fontWeight) || 400;
    const large = size >= 24 || (size >= 18.66 && weight >= 700);
    const floor = large ? 3 : 4.5;
    const value = ratio(paintedText, paintedBehind);
    const row = {
      where: name(element),
      text: text.replace(/\\s+/g, " ").trim().slice(0, 40),
      color: hex(paintedText),
      bg: hex(paintedBehind),
      ratio: Number(value.toFixed(3)),
      floor,
      size: Number(size.toFixed(1)),
      weight,
      pass: value >= floor,
    };
    if (cover.warnings.length > 0) {
      const warning = cover.warnings[0];
      row.warning = {
        selector: name(element),
        gradient: warning.gradient,
        ratio: Number(ratio(over(warning.highest, colour), over(warning.highest, behind)).toFixed(3)),
        more: cover.warnings.length - 1,
      };
    }
    return row;
  };

  const rows = [];
  for (const element of document.querySelectorAll("body *")) {
    if (element.closest("script") || element.closest("style")) continue;
    const hasText = [...element.childNodes].some(
      (node) => node.nodeType === 3 && (node.textContent ?? "").trim().length > 0,
    );
    if (!hasText) continue;
    if (!isVisible(element) || !paintsText(element)) continue;
    const colour = resolveColour(getComputedStyle(element).color);
    if (!colour || colour.a === 0) continue;
    rows.push(measure(element, element.textContent ?? "", colour));
  }

  // A closed select paints the selected option's text inside its own box. The
  // option itself has a 0x0 box, so it is measured through the select.
  for (const select of document.querySelectorAll("select")) {
    const option = select.selectedOptions[0];
    if (!option) continue;
    if (!isVisible(select) || !paintsText(select)) continue;
    const colour = resolveColour(getComputedStyle(select).color);
    if (!colour || colour.a === 0) continue;
    rows.push(measure(select, option.textContent ?? "", colour));
  }

  // Visible text only: every text node whose ancestors are all painted.
  const visibleText = (root) => {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    const parts = [];
    while (walker.nextNode()) {
      const node = walker.currentNode;
      if ((node.textContent ?? "").trim().length === 0) continue;
      let visible = true;
      for (let element = node.parentElement; element && element !== root.parentElement; element = element.parentElement) {
        if (!isVisible(element) || !paintsText(element)) {
          visible = false;
          break;
        }
      }
      if (visible) parts.push(node.textContent ?? "");
    }
    return parts.join(" ").replace(/\\s+/g, " ").trim();
  };

  const content = document.querySelector("#panel-content");
  const counts = {};
  for (const selector of counters) {
    counts[selector] = content === null
      ? 0
      : [...content.querySelectorAll(selector)].filter(isVisible).length;
  }

  const connection = document.querySelector("#connection-message");
  const probe = document.createElement("div");
  probe.style.background = "var(--bg)";
  document.body.append(probe);
  const expectedBg = getComputedStyle(probe).backgroundColor;
  probe.remove();

  return {
    rows,
    counts,
    panelText: content === null ? "" : visibleText(content),
    panelId: content?.dataset["panel"] ?? "",
    theme: document.documentElement.dataset["theme"] ?? "",
    bodyBg: getComputedStyle(document.body).backgroundColor,
    expectedBg,
    connection: {
      visible: connection !== null && isVisible(connection),
      text: connection === null ? "" : visibleText(connection),
    },
    labelCount: content === null
      ? 0
      : [...content.querySelectorAll(".mermaid p")].filter(isVisible).length,
  };
`);

const tagTabbables = new Function("selector", `${VISIBILITY_RULE}
  const nodes = [...document.querySelectorAll(selector)].filter((node) => {
    if (node.disabled) return false;
    if (node.closest("[inert]")) return false;
    const tabindex = node.getAttribute("tabindex");
    if (tabindex !== null && Number(tabindex) < 0) return false;
    return isVisible(node);
  });
  nodes.forEach((node, index) => node.setAttribute("data-audit-focus", String(index)));
  return nodes.length;
`);

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
    target,
    outline: hex(outline),
    bg: hex(acc),
    ratio: Number(value.toFixed(3)),
    floor: 3,
    pass: value >= 3,
  };
}

// Tag the focused element so the walk can tell one element from another. The
// tag is a scratch attribute; no stylesheet rule reads it.
function tagFocused() {
  const element = document.activeElement;
  if (!element || element === document.body) return null;
  let id = element.getAttribute("data-audit-focus");
  if (id === null) {
    window.__auditFocus = (window.__auditFocus ?? 0) + 1;
    id = String(window.__auditFocus);
    element.setAttribute("data-audit-focus", id);
  }
  return id;
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
  warnings: [],
  focusFailures: [],
  viewFailures: [],
  themeFailures: [],
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
    const page = await browser.newPage({ viewport: VIEWPORT });
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

    // The page must render the theme that was asked for, and the body must
    // paint that theme's --bg. A typo in the storage key would otherwise file
    // thirteen copies of one theme under thirteen names.
    const identity = await page.evaluate(() => {
      const probe = document.createElement("div");
      probe.style.background = "var(--bg)";
      document.body.append(probe);
      const expectedBg = getComputedStyle(probe).backgroundColor;
      probe.remove();
      return {
        theme: document.documentElement.dataset.theme ?? "",
        bodyBg: getComputedStyle(document.body).backgroundColor,
        expectedBg,
      };
    });
    if (identity.theme !== theme) {
      report.themeFailures.push({ theme, problem: `the page rendered ${JSON.stringify(identity.theme)}` });
    } else if (identity.bodyBg !== identity.expectedBg) {
      report.themeFailures.push({
        theme,
        problem: `the body painted ${identity.bodyBg}, not --bg ${identity.expectedBg}`,
      });
    }

    report.themes[theme] = {
      renderedTheme: identity.theme,
      bodyBg: identity.bodyBg,
      expectedBg: identity.expectedBg,
      views: {},
    };

    const measureView = async (view) => {
      // Content below the fold is painted and reachable. Size the viewport to
      // the document before measuring so the clip is the page, not the first
      // 900 px, then restore it for the next view.
      const height = await page.evaluate(() =>
        Math.max(document.documentElement.scrollHeight, document.body.scrollHeight));
      await page.setViewportSize({ width: VIEWPORT.width, height: Math.max(VIEWPORT.height, Math.ceil(height)) });
      await page.waitForTimeout(50);
      const result = await page.evaluate(auditPage, COUNTERS);
      await page.setViewportSize(VIEWPORT);
      report.themes[theme].views[view.name] = result.rows.length;
      if (!report.labelCounts.includes(result.labelCount)) report.labelCounts.push(result.labelCount);
      for (const row of result.rows.filter((entry) => !entry.pass)) {
        report.textFailures.push({ theme, view: view.name, ...row });
      }
      for (const row of result.rows) {
        if (row.warning) report.warnings.push({ theme, view: view.name, ...row.warning });
      }
      const problems = [];
      if (result.panelId !== view.panelId) {
        problems.push(`the panel id is ${JSON.stringify(result.panelId)}, expected ${JSON.stringify(view.panelId)}`);
      }
      if (view.min !== undefined && result.rows.length < view.min) {
        problems.push(`measured ${result.rows.length} visible text elements, expected at least ${view.min}`);
      }
      for (const [selector, count] of Object.entries(view.exact ?? {})) {
        const actual = result.counts[selector] ?? 0;
        if (actual !== count) problems.push(`visible ${selector}: ${actual}, expected exactly ${count}`);
      }
      for (const [selector, count] of Object.entries(view.needs ?? {})) {
        const actual = result.counts[selector] ?? 0;
        const ok = count === 0 ? actual === 0 : actual >= count;
        if (!ok) problems.push(`visible ${selector}: ${actual}, expected ${count === 0 ? "exactly 0" : `at least ${count}`}`);
      }
      for (const text of view.text ?? []) {
        if (!result.panelText.includes(text)) problems.push(`visible panel text does not include ${JSON.stringify(text)}`);
      }
      if (view.waitConnection && !result.connection.visible) {
        problems.push("the connection message is not visible");
      }
      if (view.waitConnection && !result.connection.text.includes(view.waitConnection)) {
        problems.push(`the connection message does not include ${JSON.stringify(view.waitConnection)}`);
      }
      if (problems.length > 0) report.viewFailures.push({ theme, view: view.name, problems });
    };

    // The initial view, where a scroll container is also focusable.
    await measureView(VIEWS[0]);

    // The tab order from the browser. The independent set comes from a broad
    // selector; the walk must visit every member, so a trap fails instead of
    // shrinking the walk.
    const tabbables = await page.evaluate(tagTabbables, TABBABLE_SELECTOR);
    await page.locator("body").click({ position: { x: 5, y: 5 } });
    const visited = new Set();
    let rings = 0;
    for (let step = 0; step < tabbables + 4; step += 1) {
      await page.keyboard.press("Tab");
      await page.waitForTimeout(50);
      const id = await page.evaluate(tagFocused);
      if (id === null || visited.has(id)) break;
      visited.add(id);
      const ring = await page.evaluate(focusRing);
      if (ring !== null) {
        rings += 1;
        if (!ring.pass) report.focusFailures.push({ theme, ...ring });
      }
    }
    const missing = [...Array(tabbables).keys()]
      .map(String)
      .filter((id) => !visited.has(id));
    report.focus[theme] = { tabbables, visited: visited.size, rings, missing };

    for (const view of VIEWS.slice(1)) {
      const before = await page.evaluate(() => ({
        id: document.querySelector("#panel-content")?.dataset["panel"] ?? "",
        text: document.querySelector("#panel-content")?.textContent ?? "",
      }));
      if (view.schema) {
        await page.selectOption("#schema-select", view.schema);
      }
      if (view.route === "status-disconnected") {
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
      if (view.route === "tables-abort") {
        await page.route("**/api/tables*", (route) => route.abort());
      }
      if (view.route === "tables-error") {
        await page.route("**/api/tables*", (route) => route.fulfill({
          status: 500,
          contentType: "application/json",
          body: JSON.stringify({ message: "The tables request failed." }),
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
            if (expected.connection) {
              const message = document.querySelector("#connection-message");
              if (message === null || message.hidden) return false;
              if (!(message.textContent ?? "").includes(expected.connection)) return false;
            }
            return true;
          },
          {
            text: view.waitText ?? "",
            any: view.waitAny ?? [],
            selector: view.waitSelector ?? "",
            connection: view.waitConnection ?? "",
          },
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

      // A view that clicks a tab must see the panel change, or it is measuring
      // the previous view's markup.
      if (view.panel) {
        const after = await page.evaluate(() => ({
          id: document.querySelector("#panel-content")?.dataset["panel"] ?? "",
          text: document.querySelector("#panel-content")?.textContent ?? "",
        }));
        if (after.id === before.id && after.text === before.text) {
          report.viewFailures.push({
            theme,
            view: view.name,
            problems: ["the click did not change the panel"],
          });
        }
      }

      await measureView(view);

      if (view.route) {
        await page.unroute("**/api/status*");
        await page.unroute("**/api/tables*");
      }
      if (view.route === "tables-abort" || view.route === "tables-error") {
        await page.waitForFunction(
          () => document.querySelector("#connection-message")?.hidden === true,
          undefined,
          { timeout: 15_000 },
        );
      }
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
  (total, entry) => total + Object.values(entry.views).reduce((sum, count) => sum + count, 0),
  0,
);

await writeFile(resolve(artifactRoot, "contrast.json"), JSON.stringify(report, null, 1), "utf-8");

const range = (view) => {
  const counts = Object.values(report.themes)
    .map((entry) => entry.views[view])
    .filter((count) => count !== undefined);
  return counts.length === 0 ? "0" : `${Math.min(...counts)}-${Math.max(...counts)}`;
};
const ringCounts = Object.values(report.focus).map((entry) => entry.rings);
const byBackground = new Map();
for (const [theme, entry] of Object.entries(report.themes)) {
  const group = byBackground.get(entry.bodyBg) ?? [];
  group.push(theme);
  byBackground.set(entry.bodyBg, group);
}
const shared = [...byBackground.entries()].filter(([, themes]) => themes.length > 1);

console.log(`themes: ${THEMES.length}, views: ${VIEWS.length}`);
console.log(`text elements measured: ${measured}`);
console.log(`per view: ${VIEWS.map((view) => `${view.name} ${range(view.name)}`).join(", ")}`);
console.log(`diagram label counts seen: ${report.labelCounts.join(", ")}`);
console.log(`rendered themes: ${[...new Set(Object.values(report.themes).map((entry) => entry.renderedTheme))].join(", ")}`);
console.log(`distinct body backgrounds: ${byBackground.size}`);
console.log(`tabbables: ${[...new Set(Object.values(report.focus).map((entry) => entry.tabbables))].join(", ")}`);
console.log(`tabbables visited: ${[...new Set(Object.values(report.focus).map((entry) => entry.visited))].join(", ")}`);
console.log(`focus rings per theme: ${ringCounts.length === 0 ? "0" : `${Math.min(...ringCounts)}-${Math.max(...ringCounts)}`}`);
console.log(`text failures: ${report.textFailures.length}`);
for (const item of report.textFailures.slice(0, 40)) {
  console.log(
    `  ${item.ratio} < ${item.floor}  ${item.color} on ${item.bg}  ${item.size}px/${item.weight}  ` +
      `"${item.text}"  ${item.where}  [${item.theme} ${item.view}]`,
  );
}
console.log(`gradient warnings: ${report.warnings.length}`);
for (const item of report.warnings.slice(0, 40)) {
  console.log(
    `  warning: ${item.selector}  ${item.gradient}${item.more > 0 ? ` (+${item.more} more)` : ""}  ` +
      `would read ${item.ratio} at its highest-alpha stop  [${item.theme} ${item.view}]`,
  );
}
console.log(`focus ring failures: ${report.focusFailures.length}`);
for (const item of report.focusFailures) {
  console.log(`  ${item.ratio} < ${item.floor}  ${item.outline} on ${item.bg}  ${item.target}  [${item.theme}]`);
}
console.log(`theme failures: ${report.themeFailures.length}`);
for (const item of report.themeFailures) {
  console.log(`  ${item.theme}: ${item.problem}`);
}
for (const [background, themes] of shared) {
  console.log(`  ${themes.join(" and ")} both paint ${background}; each theme must set its own --bg.`);
}
console.log(`view failures: ${report.viewFailures.length}`);
for (const item of report.viewFailures.slice(0, 20)) {
  console.log(`  ${item.view}  [${item.theme}]  ${item.problems.join("; ")}`);
}
for (const [theme, entry] of Object.entries(report.focus)) {
  if (entry.missing.length > 0 || entry.rings !== entry.visited) {
    console.log(
      `  ${theme}: ${entry.rings}/${entry.tabbables} tabbables painted a ring, ` +
        `visited ${entry.visited}, missing ${entry.missing.join(", ")}`,
    );
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
} else if (report.themeFailures.length > 0) {
  console.error("The page did not render the theme the audit asked for.");
  process.exitCode = 1;
} else if (shared.length > 0) {
  console.error("Two themes painted the same background. Each theme must set its own --bg.");
  process.exitCode = 1;
} else if (report.viewFailures.length > 0) {
  console.error("A view did not render what it must contain.");
  process.exitCode = 1;
} else if (Object.values(report.focus).some((entry) => entry.missing.length > 0 || entry.rings !== entry.visited || entry.visited === 0)) {
  console.error("A theme did not paint a ring on every tabbable element.");
  process.exitCode = 1;
} else if (measured === 0) {
  console.error("The contrast audit measured no text. The page did not render.");
  process.exitCode = 1;
} else {
  console.log("Contrast audit passed.");
}
