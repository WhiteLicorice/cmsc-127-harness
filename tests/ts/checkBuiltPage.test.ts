import { mkdir, unlink, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

// The built-page check compares each compiled module with the page in public/.
// The offline build writes two worker files that no module in src/ produces.
// These tests state that the check accepts those two files and still rejects
// any other extra JavaScript.

const root = resolve(import.meta.dirname, "../..");
const fixtureBase = resolve(root, ".vitest/check-built");

const MODULES: Record<string, string> = {
  "app.js": "console.log('app');\n",
  "panelRegistry.js": "export const panels = [];\n",
};

// A stand-in for `node_modules/typescript/bin/tsc`. It writes the modules above
// into the folder given after `--outDir`. It uses CommonJS calls, because the
// fixture folder has no package.json.
const FAKE_TSC = `const { mkdirSync, writeFileSync } = require("node:fs");
const { join } = require("node:path");
const index = process.argv.indexOf("--outDir");
const out = process.argv[index + 1];
mkdirSync(out, { recursive: true });
const modules = ${JSON.stringify(MODULES)};
for (const name of Object.keys(modules)) {
  writeFileSync(join(out, name), modules[name], "utf-8");
}
`;

// Build one page folder. `files` replaces a module body, or marks a module that
// this case leaves out with an empty body. `extra` adds a file that src/ does
// not produce.
async function fixture(
  name: string,
  options: { replace?: Record<string, string>; omit?: string[]; extra?: Record<string, string> } = {},
): Promise<string> {
  const project = resolve(fixtureBase, name);
  await mkdir(resolve(project, "node_modules/typescript/bin"), { recursive: true });
  await writeFile(resolve(project, "node_modules/typescript/bin/tsc"), FAKE_TSC, "utf-8");
  await mkdir(resolve(project, "public"), { recursive: true });
  for (const moduleName of Object.keys(MODULES)) {
    const path = resolve(project, "public", moduleName);
    if ((options.omit ?? []).includes(moduleName)) {
      await unlink(path).catch(() => undefined);
      continue;
    }
    const body = options.replace?.[moduleName] ?? MODULES[moduleName] ?? "";
    await writeFile(path, body, "utf-8");
  }
  for (const [fileName, body] of Object.entries(options.extra ?? {})) {
    await writeFile(resolve(project, "public", fileName), body, "utf-8");
  }
  return project;
}

describe("built-page check", () => {
  it("accepts the two offline worker outputs beside the compiled modules", async () => {
    const { checkBuiltPage } = await import("../../scripts/check-built-page.mjs");
    const project = await fixture("offline-outputs", {
      extra: {
        "service-worker.js": "self.addEventListener('install', () => undefined);\n",
        "service-worker.kill-switch.js": "self.addEventListener('install', () => undefined);\n",
      },
    });

    const { problems, compared } = await checkBuiltPage({ root: project });

    expect(problems).toEqual([]);
    expect(compared).toBe(2);
  });

  it("accepts a worker output left by an earlier offline build", async () => {
    // A page that ran the offline build keeps the two workers. A later clean
    // build of src/ leaves them in place. The check must still pass.
    const { checkBuiltPage } = await import("../../scripts/check-built-page.mjs");
    const project = await fixture("stale-worker", {
      extra: { "service-worker.js": "// an older worker\n" },
    });

    const { problems } = await checkBuiltPage({ root: project });

    expect(problems).toEqual([]);
  });

  it("rejects other extra JavaScript in the page", async () => {
    const { checkBuiltPage } = await import("../../scripts/check-built-page.mjs");
    const project = await fixture("extra-module", {
      extra: { "leftover.js": "console.log('old');\n" },
    });

    const { problems } = await checkBuiltPage({ root: project });

    expect(problems).toEqual([
      "public/leftover.js has no module in src/. Remove it, then rebuild the page.",
    ]);
  });

  it("rejects a module whose bytes differ from src/", async () => {
    const { checkBuiltPage } = await import("../../scripts/check-built-page.mjs");
    const project = await fixture("stale-module", {
      replace: { "app.js": "console.log('stale');\n" },
    });

    const { problems, compared } = await checkBuiltPage({ root: project });

    expect(problems).toEqual([
      "public/app.js differs from the build of src/ at line 1. Run npm run build, then commit public/.",
    ]);
    expect(compared).toBe(2);
  });

  it("rejects a module that the page does not carry", async () => {
    const { checkBuiltPage } = await import("../../scripts/check-built-page.mjs");
    const project = await fixture("missing-module", { omit: ["panelRegistry.js"] });

    const { problems } = await checkBuiltPage({ root: project });

    expect(problems).toEqual([
      "public/panelRegistry.js is missing. Run npm run build, then commit public/.",
    ]);
  });

  it("names the two files that the offline build writes", async () => {
    const { OFFLINE_OUTPUTS } = await import("../../scripts/check-built-page.mjs");
    const { WORKER_NAME, KILL_SWITCH_NAME } = await import("../../scripts/build-offline.mjs");

    expect([...OFFLINE_OUTPUTS].sort()).toEqual([KILL_SWITCH_NAME, WORKER_NAME].sort());
  });
});
