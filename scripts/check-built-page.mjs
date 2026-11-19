// Compile src/ into a scratch folder and compare each module with the page in
// public/. A stale public/app.js fails this check.
import { spawnSync } from "node:child_process";
import { mkdir, readdir, readFile, rmdir, unlink } from "node:fs/promises";
import { join, relative, resolve } from "node:path";

const root = resolve(import.meta.dirname, "..");
const buildRoot = join(root, ".build-check");
const pageRoot = join(root, "public");
const compiler = join(root, "node_modules", "typescript", "bin", "tsc");

function fail(lines) {
  for (const line of lines) {
    console.error(line);
  }
  process.exit(1);
}

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

async function removeBuild() {
  const { files, folders } = await listJavaScriptFiles(buildRoot);
  for (const file of files) {
    await unlink(file).catch(() => undefined);
  }
  for (const folder of folders.sort((left, right) => right.length - left.length)) {
    await rmdir(folder).catch(() => undefined);
  }
  await rmdir(buildRoot).catch(() => undefined);
}

function pageNames(files, base) {
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

if ((await readFile(compiler, "utf-8").catch(() => null)) === null) {
  fail(["The built-page check needs the page packages. Run npm ci first."]);
}

const pageInPublic = new Set(pageNames((await listJavaScriptFiles(pageRoot)).files, pageRoot));

const problems = [];
let compared = 0;
try {
  await mkdir(buildRoot, { recursive: true });
  const compiled = spawnSync(
    process.execPath,
    [compiler, "--outDir", buildRoot, "--pretty", "false"],
    { cwd: root, encoding: "utf-8" },
  );
  if (compiled.status !== 0) {
    fail([
      "The page did not compile, so the check cannot compare the build.",
      (compiled.stdout ?? "").trim(),
      (compiled.stderr ?? "").trim(),
    ].filter((line) => line.length > 0));
  }

  const built = pageNames((await listJavaScriptFiles(buildRoot)).files, buildRoot).sort();
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
    if (!builtNames.has(name)) {
      problems.push(`public/${name} has no module in src/. Remove it, then rebuild the page.`);
    }
  }

  if (problems.length === 0) {
    console.log(`The page in public/ matches the build of src/. ${compared} files compared.`);
  }
} finally {
  await removeBuild();
}

if (problems.length > 0) {
  fail(problems);
}
