// Compile src/ into a scratch folder and compare each module with the page in
// public/. A stale public/app.js fails this check.
//
// `npm run build:offline` writes two worker files into public/. They come from
// a template, not from a module in src/, so this check names them and skips
// them. Any other extra JavaScript still fails the check.
import { spawnSync } from "node:child_process";
import { mkdir, readdir, readFile, rmdir, unlink } from "node:fs/promises";
import { join, relative, resolve } from "node:path";

import { KILL_SWITCH_NAME, WORKER_NAME } from "./build-offline.mjs";

const root = resolve(import.meta.dirname, "..");

// The offline build writes these two files from a template. No module in src/
// produces them, so the check skips them. It still rejects any other extra
// JavaScript.
export const OFFLINE_OUTPUTS = new Set([WORKER_NAME, KILL_SWITCH_NAME]);

async function listJavaScriptFiles(directory) {
  const entries = await readdir(directory, { withFileTypes: true, recursive: true });
  const files = [];
  const folders = [];
  for (const entry of entries) {
    const full = join(entry.parentPath, entry.name);
    if (entry.isDirectory()) {
      folders.push(full);
    } else if (entry.name.endsWith(".js")) {
      files.push(full);
    }
  }
  return { files, folders };
}

async function removeBuild(buildRoot) {
  const { files, folders } = await listJavaScriptFiles(buildRoot).catch(() => ({ files: [], folders: [] }));
  for (const file of files) {
    await unlink(file).catch(() => undefined);
  }
  for (const folder of folders.sort((left, right) => right.length - left.length)) {
    await rmdir(folder).catch(() => undefined);
  }
  await rmdir(buildRoot).catch(() => undefined);
}

function moduleNames(files, base) {
  return files
    .map((file) => relative(base, file).split("\\").join("/"))
    .filter((name) => !name.startsWith("vendor/"));
}

function firstDifferingLine(expected, actual) {
  const left = expected.split("\n");
  const right = actual.split("\n");
  const limit = Math.max(left.length, right.length);
  for (let index = 0; index < limit; index += 1) {
    if (left[index] !== right[index]) {
      return index + 1;
    }
  }
  return 1;
}

// Compare the page in `pageRoot` with a fresh compile of `src/`. Return the
// problems and the number of modules compared. The caller prints and exits.
export async function checkBuiltPage(options = {}) {
  const project = resolve(options.root ?? root);
  const pageRoot = join(project, "public");
  const buildRoot = join(project, ".build-check");
  const compiler = join(project, "node_modules", "typescript", "bin", "tsc");
  const problems = [];
  let compared = 0;
  try {
    if ((await readFile(compiler, "utf-8").catch(() => null)) === null) {
      problems.push("The built-page check needs the page packages. Run npm ci first.");
    } else {
      await mkdir(buildRoot, { recursive: true });
      const compiled = spawnSync(
        process.execPath,
        [compiler, "--outDir", buildRoot, "--pretty", "false"],
        { cwd: project, encoding: "utf-8" },
      );
      if (compiled.status !== 0) {
        problems.push("The page did not compile, so the check cannot compare the build.");
        for (const line of `${compiled.stdout}${compiled.stderr}`.trim().split("\n")) {
          problems.push(line);
        }
      } else {
        const pageInPublic = new Set(moduleNames((await listJavaScriptFiles(pageRoot)).files, pageRoot));
        const built = moduleNames((await listJavaScriptFiles(buildRoot)).files, buildRoot).sort();
        const builtNames = new Set(built);

        for (const name of built) {
          if (!pageInPublic.has(name)) {
            problems.push(`public/${name} is missing. Run npm run build, then commit public/.`);
            continue;
          }
          const expected = await readFile(join(buildRoot, name), "utf-8");
          const actual = await readFile(join(pageRoot, name), "utf-8");
          compared += 1;
          if (expected !== actual) {
            problems.push(
              `public/${name} differs from the build of src/ at line ${firstDifferingLine(expected, actual)}. `
                + "Run npm run build, then commit public/.",
            );
          }
        }

        for (const name of [...pageInPublic].sort()) {
          if (builtNames.has(name)) continue;
          if (OFFLINE_OUTPUTS.has(name)) continue;
          problems.push(`public/${name} has no module in src/. Remove it, then rebuild the page.`);
        }
      }
    }
  } finally {
    await removeBuild(buildRoot);
  }
  return { problems, compared };
}

if (process.argv[1] && resolve(process.argv[1]) === resolve(import.meta.filename)) {
  const { problems, compared } = await checkBuiltPage();
  if (problems.length > 0) {
    for (const line of problems) {
      console.error(line);
    }
    process.exit(1);
  }
  console.log(`The page in public/ matches the build of src/. ${compared} files compared.`);
}
