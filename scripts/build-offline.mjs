// Write the offline snapshot files into the built page.
//
// The page deploys from `public/`. This script writes three files there:
//
//   offline-manifest.json          every page file with its SHA-256 hash
//   service-worker.js              the worker, with its build ID filled in
//   service-worker.kill-switch.js  the worker that clears the snapshot
//
// The build ID is a hash of the manifest and the worker template. The same
// page and the same template always give the same build ID.

import { createHash } from "node:crypto";
import { readdir, readFile, writeFile } from "node:fs/promises";
import { join, resolve, sep } from "node:path";

export const SCHEMA_VERSION = 1;
export const MANIFEST_NAME = "offline-manifest.json";
export const WORKER_NAME = "service-worker.js";
export const KILL_SWITCH_NAME = "service-worker.kill-switch.js";
const BUILD_ID_PLACEHOLDER = '"__OFFLINE_BUILD_ID__"';

export const templatePath = resolve(import.meta.dirname, "../src/service-worker.template.js");
export const killSwitchTemplatePath = resolve(
  import.meta.dirname,
  "../src/service-worker.kill-switch.template.js",
);

// Files this script writes. They are not page content, so no snapshot entry
// names them.
const GENERATED = new Set([MANIFEST_NAME, WORKER_NAME, KILL_SWITCH_NAME]);

function sha256(bytes) {
  return createHash("sha256").update(bytes).digest("hex");
}

function portable(path) {
  return path.split(sep).join("/");
}

async function walk(directory, prefix = "") {
  const entries = await readdir(directory, { withFileTypes: true });
  const files = [];
  for (const entry of entries) {
    const name = portable(join(prefix, entry.name));
    if (prefix === "" && GENERATED.has(name)) continue;
    if (entry.isDirectory()) {
      files.push(...(await walk(join(directory, entry.name), name)));
    } else if (entry.isFile()) {
      files.push(name);
    }
  }
  return files.sort();
}

async function pageEntries(directory) {
  const entries = [];
  for (const path of await walk(directory)) {
    entries.push({ path, hash: sha256(await readFile(join(directory, path))) });
  }
  return entries;
}

export async function writeOfflineFiles(directory, options = {}) {
  const target = resolve(directory);
  const template = options.template ?? (await readFile(templatePath, "utf-8"));
  const killSwitch = options.killSwitch ?? (await readFile(killSwitchTemplatePath, "utf-8"));
  const files = await pageEntries(target);

  if (!files.some((entry) => entry.path === "index.html")) {
    throw new Error(`The page folder has no index.html: ${target}`);
  }
  if (!template.includes(BUILD_ID_PLACEHOLDER)) {
    throw new Error("The worker template has no build ID placeholder.");
  }

  // Hash the manifest body with the template. The build ID is not part of the
  // body, so the hash never folds into itself.
  const body = JSON.stringify({ schemaVersion: SCHEMA_VERSION, files });
  const buildId = sha256(`${body}\n${template}`);
  const manifest = JSON.stringify({ schemaVersion: SCHEMA_VERSION, buildId, files });

  await writeFile(join(target, MANIFEST_NAME), `${manifest}\n`, "utf-8");
  await writeFile(
    join(target, WORKER_NAME),
    template.replace(BUILD_ID_PLACEHOLDER, JSON.stringify(buildId)),
    "utf-8",
  );
  await writeFile(join(target, KILL_SWITCH_NAME), killSwitch, "utf-8");

  return { buildId, files: files.map((entry) => entry.path), manifest };
}

if (process.argv[1] && resolve(process.argv[1]) === resolve(import.meta.filename)) {
  const directory = process.argv[2] ?? resolve(import.meta.dirname, "../public");
  const result = await writeOfflineFiles(directory);
  console.log(`Wrote ${result.files.length} page files and build ${result.buildId}.`);
}
