import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const root = resolve(import.meta.dirname, "../..");
const css = readFileSync(resolve(root, "public/style.css"), "utf-8");
const html = readFileSync(resolve(root, "public/index.html"), "utf-8");

const THEMES = [
  "upv-light",
  "upv-dark",
  "github-light",
  "github-dark",
  "one-dark",
  "nord",
  "solarized-light",
  "solarized-dark",
  "catppuccin-latte",
  "catppuccin-mocha",
  "tokyo-night",
  "gruvbox-light",
  "gruvbox-dark",
];

const DARK = new Set([
  "upv-dark",
  "github-dark",
  "one-dark",
  "nord",
  "solarized-dark",
  "catppuccin-mocha",
  "tokyo-night",
  "gruvbox-dark",
]);

const TOKENS = [
  "--bg",
  "--surface",
  "--surface-2",
  "--code-bg",
  "--text",
  "--muted",
  "--border",
  "--rule",
  "--header-bg",
  "--header-text",
  "--header-muted",
  "--focus",
  "--focus-header",
  "--accent",
  "--pass",
  "--fail",
  "--warn",
  "--info",
  "--pass-bg",
  "--fail-bg",
  "--warn-bg",
  "--info-bg",
  "--row-insert",
  "--row-update",
  "--row-delete",
];

type TokenMap = Record<string, string>;

function themeBlock(id: string): string {
  const pattern = new RegExp(`\\[data-theme="${id}"\\]\\s*\\{([^}]*)\\}`);
  const match = pattern.exec(css);
  return match?.[1] ?? "";
}

function readTokens(block: string): TokenMap {
  const tokens: TokenMap = {};
  for (const line of block.split(";")) {
    const match = /^\s*(--[a-z0-9-]+)\s*:\s*([^;]+?)\s*$/.exec(line);
    if (match?.[1] !== undefined && match[2] !== undefined) {
      tokens[match[1]] = match[2];
    }
  }
  return tokens;
}

function readScheme(block: string): string {
  return /color-scheme\s*:\s*([a-z]+)/.exec(block)?.[1] ?? "";
}

function channels(hex: string): [number, number, number] {
  const clean = hex.replace("#", "");
  const full =
    clean.length === 3
      ? clean
          .split("")
          .map((part) => part + part)
          .join("")
      : clean;
  return [
    Number.parseInt(full.slice(0, 2), 16),
    Number.parseInt(full.slice(2, 4), 16),
    Number.parseInt(full.slice(4, 6), 16),
  ];
}

function linear(value: number): number {
  const scaled = value / 255;
  return scaled <= 0.03928 ? scaled / 12.92 : ((scaled + 0.055) / 1.055) ** 2.4;
}

function luminance(hex: string): number {
  const [red, green, blue] = channels(hex);
  return 0.2126 * linear(red) + 0.7152 * linear(green) + 0.0722 * linear(blue);
}

function contrast(foreground: string, background: string): number {
  const first = luminance(foreground);
  const second = luminance(background);
  const [high, low] = first > second ? [first, second] : [second, first];
  return (high + 0.05) / (low + 0.05);
}

describe("theme tokens", () => {
  it("defines every required theme with the full token set", () => {
    const missing: string[] = [];
    for (const id of THEMES) {
      const tokens = readTokens(themeBlock(id));
      for (const token of TOKENS) {
        if (!/^#[0-9a-f]{6}$/.test(tokens[token] ?? "")) {
          missing.push(`${id} ${token}`);
        }
      }
    }
    expect(missing).toEqual([]);
  });

  it("sets color-scheme from the theme's own brightness", () => {
    const wrong: string[] = [];
    for (const id of THEMES) {
      const scheme = readScheme(themeBlock(id));
      const expected = DARK.has(id) ? "dark" : "light";
      if (scheme !== expected) {
        wrong.push(`${id}: ${scheme || "unset"} (expected ${expected})`);
      }
    }
    expect(wrong).toEqual([]);
  });

  it("keeps print light in every screen theme", () => {
    const print = /@media print\s*\{([\s\S]*?)\n\}/.exec(css)?.[1] ?? "";
    expect(print).toContain("color-scheme: light");
    const tokens = readTokens(print);
    expect(tokens["--bg"]).toBe("#f7f3ee");
    expect(tokens["--text"]).toBe("#241a16");
  });

  it("holds no literal colour outside a token block", () => {
    const withoutTokens = css.replace(/\{[^}]*--bg:[^}]*\}/g, "{}");
    const literals = withoutTokens.match(/#[0-9a-fA-F]{3,8}\b/g) ?? [];
    expect(literals).toEqual([]);
  });

  it("uses the guidebook UP maroon, forest green, and gold", () => {
    for (const id of ["upv-light", "upv-dark"]) {
      const tokens = readTokens(themeBlock(id));
      expect(tokens["--header-bg"]).toBe("#8a1538");
    }
    const light = readTokens(themeBlock("upv-light"));
    expect(light["--accent"]).toBe("#00573f");
    expect(light["--fail"]).toBe("#8a1538");
    expect(light["--focus-header"]).toBe("#ffb81c");
    const dark = readTokens(themeBlock("upv-dark"));
    expect(dark["--focus"]).toBe("#ffb81c");
    expect(dark["--warn"]).toBe("#ffb81c");
  });
});

describe("theme contrast", () => {
  // Every foreground and background pair the page really renders, with the
  // surface that carries it. A pair belongs here when a rule paints that
  // foreground on that background somewhere in the page.
  const pairs: Array<[string, string, number, string]> = [
    ["--text", "--surface", 4.5, "panel body, theme and schema pickers, diagram odd rows"],
    ["--text", "--bg", 4.5, "page body"],
    ["--text", "--surface-2", 4.5, "table head, diagram title and even rows, active tab"],
    ["--text", "--code-bg", 4.5, "code block"],
    ["--text", "--row-insert", 4.5, "insert row"],
    ["--text", "--row-update", 4.5, "update row"],
    ["--text", "--row-delete", 4.5, "delete row"],
    ["--muted", "--surface", 4.5, "panel prose and table captions"],
    ["--muted", "--bg", 4.5, "poll summary"],
    ["--surface", "--accent", 4.5, "text selection"],
    ["--bg", "--accent", 4.5, "button hover"],
    ["--border", "--surface", 3, "control edge"],
    ["--border", "--bg", 3, "control edge on the page"],
    ["--rule", "--surface", 1.25, "table rule"],
    ["--pass", "--surface", 4.5, "pass status"],
    ["--fail", "--surface", 4.5, "fail status and error message"],
    ["--warn", "--surface", 4.5, "warning status"],
    ["--info", "--surface", 4.5, "skip status"],
    ["--pass", "--pass-bg", 4.5, "pass status cell"],
    ["--fail", "--fail-bg", 4.5, "fail status cell and error message"],
    ["--warn", "--warn-bg", 4.5, "warning note"],
    ["--info", "--info-bg", 4.5, "skip status cell"],
    ["--header-text", "--header-bg", 4.5, "header title"],
    ["--header-muted", "--header-bg", 4.5, "header byline"],
    ["--focus", "--bg", 3, "focus ring on the page"],
    ["--focus", "--surface", 3, "focus ring on a panel"],
    ["--focus-header", "--header-bg", 3, "focus ring in the header"],
  ];

  it("meets the floor for every pair the page renders in every theme", () => {
    const failures: string[] = [];
    for (const id of THEMES) {
      const tokens = readTokens(themeBlock(id));
      for (const [foreground, background, floor, where] of pairs) {
        const first = tokens[foreground];
        const second = tokens[background];
        if (first === undefined || second === undefined) {
          failures.push(`${id} ${foreground}/${background} (${where}): token missing`);
          continue;
        }
        const ratio = contrast(first, second);
        if (ratio < floor) {
          failures.push(`${id} ${foreground}/${background} (${where}) ${ratio.toFixed(2)} < ${floor}`);
        }
      }
    }
    expect(failures).toEqual([]);
  });

  it("keeps the four state colours distinct from each other", () => {
    const failures: string[] = [];
    for (const id of THEMES) {
      const tokens = readTokens(themeBlock(id));
      const states = ["--pass", "--fail", "--warn", "--info"].map((token) => tokens[token] ?? "");
      const unique = new Set(states.map((value) => value.toLowerCase()));
      if (unique.size !== 4) {
        failures.push(`${id}: ${states.join(" ")}`);
      }
    }
    expect(failures).toEqual([]);
  });

  // 1.15 is the smallest step the published palettes reach. Nord's own pair,
  // nord6 on nord4, sits at 1.17, so a higher floor would force an invented tone.
  it("keeps muted text visibly apart from body text in every theme", () => {
    const failures: string[] = [];
    for (const id of THEMES) {
      const tokens = readTokens(themeBlock(id));
      const ratio = contrast(tokens["--muted"] ?? "", tokens["--text"] ?? "");
      if (ratio < 1.15) {
        failures.push(`${id} ${ratio.toFixed(2)} < 1.15`);
      }
    }
    expect(failures).toEqual([]);
  });

  // 1.06 is the smallest band the published themes already use. Catppuccin
  // Mocha and Tokyo Night sit at 1.07.
  it("separates the header band from the page in every theme", () => {
    const failures: string[] = [];
    for (const id of THEMES) {
      const tokens = readTokens(themeBlock(id));
      const ratio = contrast(tokens["--header-bg"] ?? "", tokens["--bg"] ?? "");
      if (ratio < 1.06) {
        failures.push(`${id} ${ratio.toFixed(2)} < 1.06`);
      }
    }
    expect(failures).toEqual([]);
  });

  // A new colour rule must arrive with a pair in the table above, or this fails.
  // This reads tokens only. It cannot see which background a rule paints on, so
  // scripts/contrast-audit.mjs is the guard that checks the rendered page.
  it("gives every `color: var(--x)` token a pair", () => {
    const declared = new Set(
      [...css.matchAll(/(?<![-\w])color:\s*var\((--[a-z0-9-]+)\)/g)].map((match) => match[1] ?? ""),
    );
    const covered = new Set(pairs.map(([foreground]) => foreground));
    const missing = [...declared].filter((token) => !covered.has(token));
    expect(missing).toEqual([]);
  });

  // The diagram labels sit on --surface-2, not on --surface. A rule that lets
  // the panel prose colour reach them drops five themes below 4.5:1.
  it("paints the diagram label with the body text colour", () => {
    const rule = /\.mermaid p\s*\{([^}]*)\}/.exec(css)?.[1] ?? "";
    expect(rule).toMatch(/(?<![-\w])color:\s*var\(--text\)/);
  });
});

describe("page header", () => {
  it("shows the byline with a GitHub mark that links to the repository", () => {
    expect(html).toContain("by WhiteLicorice");
    expect(html).toContain('href="https://github.com/WhiteLicorice/cmsc-127-harness"');
    expect(html).toContain('rel="noopener noreferrer"');
    expect(html).toContain('target="_blank"');
    expect(html).toContain('aria-label="WhiteLicorice on GitHub"');
    expect(html).toMatch(/<svg[^>]*aria-hidden="true"/);
    expect(html).toMatch(/<svg[\s\S]*?currentColor/);
  });

  it("drops the old subtitle", () => {
    expect(html).not.toContain("Read-only view of committed PostgreSQL data");
  });

  it("offers a labelled theme picker grouped by family", () => {
    expect(html).toMatch(/<label[^>]*for="theme-select"[^>]*>Theme<\/label>/);
    expect(html).toMatch(/<select id="theme-select"/);
    for (const group of ["UP", "GitHub", "Community"]) {
      expect(html).toContain(`<optgroup label="${group}">`);
    }
    for (const id of THEMES) {
      expect(html).toContain(`value="${id}"`);
    }
  });

  it("applies the stored theme from the head before the stylesheet paints", () => {
    const head = /<head>([\s\S]*?)<\/head>/.exec(html)?.[1] ?? "";
    const script = /<script(?![^>]*\b(?:defer|async|type="module")\b)[^>]*>([\s\S]*?)<\/script>/.exec(head);
    expect(script).not.toBeNull();
    const body = script?.[1] ?? "";
    expect(body).toContain("localStorage");
    expect(body).toContain("prefers-color-scheme");
    const scriptAt = head.indexOf(script?.[0] ?? "");
    const styleAt = head.indexOf('<link rel="stylesheet"');
    expect(scriptAt).toBeGreaterThanOrEqual(0);
    expect(styleAt).toBeGreaterThan(scriptAt);
  });
});
