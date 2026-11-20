import { spawn } from "node:child_process";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { describe, expect, it } from "vitest";

// The smoke scripts start the harness server on a port of their own. A gate
// that runs beside another gate must reserve its own port, so each script
// passes an explicit port through from its own environment and keeps its old
// value as the default.
//
// Each case runs the script with a recorder in front of child_process. The
// recorder answers the fixture SQL, writes the port that the script asked its
// server for, and ends the run. No server and no browser start.

const root = resolve(import.meta.dirname, "../..");
const fixtureBase = resolve(root, ".vitest/smoke-port");

const RECORDER = `import childProcess from "node:child_process";
import { writeFileSync } from "node:fs";
import { syncBuiltinESMExports } from "node:module";

const out = process.env.SMOKE_PORT_RECORD;

function record(port) {
  writeFileSync(out, String(port ?? "none"), "utf-8");
  process.exit(0);
}

// A child that reports a clean exit, so the fixture SQL step moves on.
function quietChild() {
  const child = {
    stdout: { on() {} },
    stderr: { on() {} },
    exitCode: 0,
    kill() {},
    on(name, handler) {
      if (name === "exit") queueMicrotask(() => handler(0));
      return child;
    },
  };
  return child;
}

childProcess.spawnSync = () => ({ status: 0, stdout: "", stderr: "" });
childProcess.spawn = (command, args, options) => {
  const list = Array.isArray(args) ? args : [];
  if (!list.some((arg) => String(arg).endsWith("server.py"))) return quietChild();
  record(options?.env?.CMSC127_PORT);
};
syncBuiltinESMExports();
`;

const SCRIPTS = ["browser-smoke.mjs", "schema-startup-smoke.mjs", "contrast-audit.mjs"];

async function requestedPort(script: string, port: string | undefined): Promise<string> {
  const folder = resolve(fixtureBase, script.replace(/\W/g, "-"));
  await mkdir(folder, { recursive: true });
  const recorder = resolve(folder, "recorder.mjs");
  await writeFile(recorder, RECORDER, "utf-8");
  const record = resolve(folder, "port.txt");
  const env: Record<string, string> = {
    ...(process.env as Record<string, string>),
    SMOKE_PORT_RECORD: record,
    CMSC127_DATABASE_URL: "postgresql://postgres@127.0.0.1:1/none",
    CMSC127_PAIR_DATABASE_URL: "postgresql://postgres@127.0.0.1:1/none",
  };
  if (port === undefined) delete env["CMSC127_PORT"];
  else env["CMSC127_PORT"] = port;
  const child = spawn(
    process.execPath,
    ["--import", pathToFileURL(recorder).href, resolve(root, "scripts", script)],
    { cwd: root, env, stdio: "ignore" },
  );
  await new Promise((done) => child.on("exit", done));
  return (await readFile(record, "utf-8")).trim();
}

describe("smoke script port", () => {
  for (const script of SCRIPTS) {
    it(`${script} passes an explicit port through`, async () => {
      expect(await requestedPort(script, "55691")).toBe("55691");
    });

    it(`${script} keeps its own port when none is given`, async () => {
      const port = await requestedPort(script, undefined);
      expect(port).not.toBe("none");
      expect(Number(port)).toBeGreaterThan(0);
    });
  }
});
