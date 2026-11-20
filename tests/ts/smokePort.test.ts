import { spawn } from "node:child_process";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { createServer } from "node:http";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { afterEach, describe, expect, it } from "vitest";
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

// The harness reserves its test ports through CMSC127_TEST_PORTS, the same
// variable the Python tests read, so no gate's port is fixed in this file.
// Every port this returns comes from that list. A smoke script never binds
// outside the range the gate reserved. A value that holds no port number
// stops the run with a message. The gate then reads the fault. Without that
// variable, ask the operating system for a free port.
async function reservedPort(): Promise<number> {
  const listed = process.env["CMSC127_TEST_PORTS"] ?? "";
  if (listed.trim() !== "") {
    for (const entry of listed.split(",")) {
      const text = entry.trim();
      if (text === "") continue;
      if (!/^\d+$/.test(text)) {
        throw new Error(`CMSC127_TEST_PORTS holds ${JSON.stringify(text)}, which is not a port number.`);
      }
      const port = Number(text);
      if (!(port >= 1 && port <= 65535)) {
        throw new Error(`CMSC127_TEST_PORTS holds ${port}, which is outside 1 to 65535.`);
      }
      return port;
    }
    throw new Error("CMSC127_TEST_PORTS holds no port. This fixture needs 1.");
  }
  const probe = createServer();
  await new Promise<void>((ready) => probe.listen(0, "127.0.0.1", ready));
  const address = probe.address();
  const port = typeof address === "object" && address !== null ? address.port : 0;
  await new Promise<void>((done) => probe.close(() => done()));
  return port;
}

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
      const port = String(await reservedPort());
      expect(await requestedPort(script, port)).toBe(port);
    });

    it(`${script} keeps its own port when none is given`, async () => {
      const port = await requestedPort(script, undefined);
      expect(port).not.toBe("none");
      expect(Number(port)).toBeGreaterThan(0);
    });
  }
});

// A gate that reserves its ports must not have a fixture bind outside them. A
// list that cannot serve the fixture stops the run and states the fault.
describe("reserved port list", () => {
  const saved = process.env["CMSC127_TEST_PORTS"];

  afterEach(() => {
    if (saved === undefined) delete process.env["CMSC127_TEST_PORTS"];
    else process.env["CMSC127_TEST_PORTS"] = saved;
  });

  it("takes the first port of the list", async () => {
    process.env["CMSC127_TEST_PORTS"] = "55736,55737";
    expect(await reservedPort()).toBe(55736);
  });

  it("stops on a port above 65535 and names the value", async () => {
    process.env["CMSC127_TEST_PORTS"] = "99999,55737";
    await expect(reservedPort()).rejects.toThrow("99999");
  });

  it("stops on a port of zero and names the value", async () => {
    process.env["CMSC127_TEST_PORTS"] = "0,55737";
    await expect(reservedPort()).rejects.toThrow("0");
  });

  it("stops on a value that is not a number and names it", async () => {
    process.env["CMSC127_TEST_PORTS"] = "http,55737";
    await expect(reservedPort()).rejects.toThrow("http");
  });

  it("stops on a list that holds no port", async () => {
    process.env["CMSC127_TEST_PORTS"] = " , ";
    await expect(reservedPort()).rejects.toThrow("needs 1");
  });

  it("asks the operating system when the variable is unset", async () => {
    delete process.env["CMSC127_TEST_PORTS"];
    const port = await reservedPort();
    expect(port).toBeGreaterThan(0);
    expect(port).toBeLessThan(65536);
  });
});
