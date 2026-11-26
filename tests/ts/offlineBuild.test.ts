import { createHash } from "node:crypto";
import { mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

// The offline build writes the manifest and the worker into the built page.
// These tests state what it must do.

const root = resolve(import.meta.dirname, "../..");
const fixtureRoot = resolve(root, ".vitest/offline-fixture");

async function fixture(directory: string, appSource = "export const a = 1;\n"): Promise<string> {
  await mkdir(resolve(directory, "vendor"), { recursive: true });
  await writeFile(resolve(directory, "index.html"), "<!doctype html><title>a</title>\n", "utf-8");
  await writeFile(resolve(directory, "app.js"), appSource, "utf-8");
  await writeFile(resolve(directory, "vendor", "mermaid.min.js"), "// mermaid\n", "utf-8");
  return directory;
}

describe("offline build", () => {
  it("writes a manifest with one hash per page file", async () => {
    const { writeOfflineFiles } = await import("../../scripts/build-offline.mjs");
    const target = await fixture(fixtureRoot);
    const result = await writeOfflineFiles(target);
    const manifest = JSON.parse(await readFile(resolve(target, "offline-manifest.json"), "utf-8"));
    expect(manifest.files.map((entry: { path: string }) => entry.path)).toEqual([
      "app.js", "index.html", "vendor/mermaid.min.js",
    ]);
    const app = await readFile(resolve(target, "app.js"));
    expect(manifest.files[0].hash).toBe(createHash("sha256").update(app).digest("hex"));
    expect(manifest.buildId).toBe(result.buildId);
    expect(result.buildId).toMatch(/^[0-9a-f]{64}$/);
    const worker = await readFile(resolve(target, "service-worker.js"), "utf-8");
    expect(worker).toContain(`"${result.buildId}"`);
    expect(worker).not.toContain("__OFFLINE_BUILD_ID__");
  });

  it("writes the same bytes for the same page", async () => {
    const { writeOfflineFiles } = await import("../../scripts/build-offline.mjs");
    const target = await fixture(fixtureRoot);
    const first = await writeOfflineFiles(target);
    const firstManifest = await readFile(resolve(target, "offline-manifest.json"), "utf-8");
    const second = await writeOfflineFiles(target);
    expect(second.buildId).toBe(first.buildId);
    expect(await readFile(resolve(target, "offline-manifest.json"), "utf-8")).toBe(firstManifest);
  });

  it("keeps generated files out of the manifest", async () => {
    const { writeOfflineFiles } = await import("../../scripts/build-offline.mjs");
    const target = await fixture(fixtureRoot);
    const first = await writeOfflineFiles(target);
    const second = await writeOfflineFiles(target);
    const manifest = JSON.parse(await readFile(resolve(target, "offline-manifest.json"), "utf-8"));
    const paths = manifest.files.map((entry: { path: string }) => entry.path);
    expect(paths).not.toContain("service-worker.js");
    expect(paths).not.toContain("offline-manifest.json");
    expect(second.buildId).toBe(first.buildId);
  });

  it("leaves the version stamp out of the manifest and the build ID", async () => {
    const { writeOfflineFiles } = await import("../../scripts/build-offline.mjs");
    const target = await fixture(resolve(root, ".vitest/offline-stamp"));
    await rm(resolve(target, "version.json"), { force: true });
    const first = await writeOfflineFiles(target);
    await writeFile(
      resolve(target, "version.json"),
      '{"commit":"1111111111111111111111111111111111111111","run":"1","attempt":"1"}\n',
      "utf-8",
    );
    const second = await writeOfflineFiles(target);
    const manifest = JSON.parse(await readFile(resolve(target, "offline-manifest.json"), "utf-8"));
    const paths = manifest.files.map((entry: { path: string }) => entry.path);
    expect(paths).not.toContain("version.json");
    expect(second.buildId).toBe(first.buildId);
  });

  it("changes the build ID when one page file changes", async () => {
    const { writeOfflineFiles } = await import("../../scripts/build-offline.mjs");
    const first = await writeOfflineFiles(await fixture(fixtureRoot));
    const second = await writeOfflineFiles(await fixture(fixtureRoot, "export const a = 2;\n"));
    expect(second.buildId).not.toBe(first.buildId);
  });

  it("writes the kill-switch worker beside the worker", async () => {
    const { writeOfflineFiles } = await import("../../scripts/build-offline.mjs");
    const target = await fixture(fixtureRoot);
    await writeOfflineFiles(target);
    const worker = await readFile(resolve(target, "service-worker.kill-switch.js"), "utf-8");
    expect(worker).toContain("cmsc127-harness-");
    expect(worker).toContain("unregister");
  });

  it("refuses a page folder with no index.html", async () => {
    const { writeOfflineFiles } = await import("../../scripts/build-offline.mjs");
    const target = resolve(root, ".vitest/offline-empty");
    await mkdir(target, { recursive: true });
    await expect(writeOfflineFiles(target)).rejects.toThrow(/index\.html/);
  });
});
