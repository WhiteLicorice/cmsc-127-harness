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

// The port rule. tests/test_server.py holds the same rule and the same cases,
// so one rule covers both languages.
//
// Absent: fall back to the default only when the variable is absent.
//
// Splitting and trimming: split on a comma. Trim only ASCII space and tab from
// each entry. No other character counts as whitespace. String.trim() is not
// used. It also trims U+0085 and U+FEFF.
//
// Valid entry: each entry matches ^[0-9]{1,5}$ and its value is 1 to 65535. A
// sixth digit, leading zeros past five digits, U+0085, U+FEFF, and a non-ASCII
// digit are all faults with a message.
//
// Order of checks. An empty or whitespace-only value is a fault. Count the
// distinct ports before the required count. Validate the whole list before
// choosing any port.
const ASCII_DIGITS = /^[0-9]{1,5}$/;
const TRIM = /^[ \t]+|[ \t]+$/g;

function listedPorts(listed: string): number[] {
  if (listed.replace(/[ \t]/g, "") === "") {
    throw new Error("CMSC127_TEST_PORTS holds an empty entry. Give a port number for each entry.");
  }
  const ports: number[] = [];
  for (const entry of listed.split(",")) {
    const text = entry.replace(TRIM, "");
    if (text === "") {
      throw new Error("CMSC127_TEST_PORTS holds an empty entry. Give a port number for each entry.");
    }
    if (!ASCII_DIGITS.test(text)) {
      throw new Error(`CMSC127_TEST_PORTS holds ${JSON.stringify(text)}, which is not a port number.`);
    }
    const port = Number(text);
    if (!(port >= 1 && port <= 65535)) {
      throw new Error(`CMSC127_TEST_PORTS holds ${port}, which is outside 1 to 65535.`);
    }
    ports.push(port);
  }
  return ports;
}

// Every port this returns comes from the reserved list. No port repeats.
// A smoke script never binds outside the range the gate reserved. A list that
// holds too few distinct ports stops the run with a message. The gate then
// reads the fault. Without that variable, ask the operating system.
async function reservedPorts(count: number): Promise<number[]> {
  const listed = process.env["CMSC127_TEST_PORTS"];
  if (listed !== undefined) {
    const chosen = [...new Set(listedPorts(listed))];
    if (chosen.length < count) {
      throw new Error(
        `CMSC127_TEST_PORTS lists ${chosen.length} distinct ports. `
        + `This fixture needs ${count}. Add ${count - chosen.length} more.`,
      );
    }
    return chosen.slice(0, count);
  }
  const ports: number[] = [];
  while (ports.length < count) {
    const probe = createServer();
    await new Promise<void>((ready) => probe.listen(0, "127.0.0.1", ready));
    const address = probe.address();
    const port = typeof address === "object" && address !== null ? address.port : 0;
    await new Promise<void>((done) => probe.close(() => done()));
    if (!ports.includes(port)) ports.push(port);
  }
  return ports;
}

async function reservedPort(): Promise<number> {
  const [port] = await reservedPorts(1);
  if (port === undefined) {
    throw new Error("CMSC127_TEST_PORTS holds no port. This fixture needs 1.");
  }
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

// One table for both languages. tests/test_server.py reads the same cases, so
// one rule covers both. Each row is the value, the fault text it must raise,
// and the ports it must return. An empty fault text means the value is good.
const PORT_LIST_CASES: [string, string, number[]][] = [
  ["55740,55741", "", [55740, 55741]],
  [" 55740 , 55741 ", "", [55740, 55741]],
  ["\t55740\t,\t55741\t", "", [55740, 55741]],
  ["1", "", [1]],
  ["65535", "", [65535]],
  ["00001", "", [1]],
  ["55740,55740", "", [55740, 55740]],
  ["", "empty entry", []],
  [" ", "empty entry", []],
  ["\t", "empty entry", []],
  ["55740,,55741", "empty entry", []],
  ["55740,", "empty entry", []],
  ["55740,99999", "outside 1 to 65535", []],
  ["0", "outside 1 to 65535", []],
  ["65536", "outside 1 to 65535", []],
  ["\u00b2", "not a port number", []],
  ["55740,\u00b2", "not a port number", []],
  ["http", "not a port number", []],
  ["+55740", "not a port number", []],
  ["55740.0", "not a port number", []],
  ["\u008555740", "not a port number", []],
  ["55740\u0085", "not a port number", []],
  ["\ufeff55740", "not a port number", []],
  ["55740\ufeff", "not a port number", []],
  ["000001", "not a port number", []],
  ["0".repeat(4300) + "55740", "not a port number", []],
];

// The same table for the count rule. Each row holds the value, the count the
// fixture needs, and the expected result. The result is either the ports it
// must return or the fault text it must raise. A value of null means the
// variable is absent.
const PORT_RESERVE_CASES: [string | null, number, number[] | string][] = [
  ["55740,55741,55742", 3, [55740, 55741, 55742]],
  ["55740,55741", 3, "distinct ports"],
  ["55740,55740,55741", 2, [55740, 55741]],
  ["55740,55740", 2, "distinct ports"],
  ["55740,55740,55740,55740,55740,55740", 6, "distinct ports"],
  [null, 3, "operating system"],
  [" ", 1, "empty entry"],
  ["55740,99999", 1, "outside 1 to 65535"],
  ["\u008555740", 1, "not a port number"],
  ["\ufeff55740", 1, "not a port number"],
  ["0".repeat(4300) + "55740", 1, "not a port number"],
];

// A gate that reserves its ports must not have a fixture bind outside them. A
// list that cannot serve the fixture stops the run and states the fault.
describe("reserved port list", () => {
  const saved = process.env["CMSC127_TEST_PORTS"];

  afterEach(() => {
    if (saved === undefined) delete process.env["CMSC127_TEST_PORTS"];
    else process.env["CMSC127_TEST_PORTS"] = saved;
  });

  it("reads every row of the shared parse table", () => {
    for (const [listed, fault, expected] of PORT_LIST_CASES) {
      if (fault === "") {
        expect(listedPorts(listed), listed).toEqual(expected);
      } else {
        expect(() => listedPorts(listed), listed).toThrow(fault);
      }
    }
  });

  it("reads every row of the shared count table", async () => {
    for (const [listed, count, expected] of PORT_RESERVE_CASES) {
      if (listed === null) delete process.env["CMSC127_TEST_PORTS"];
      else process.env["CMSC127_TEST_PORTS"] = listed;
      if (typeof expected === "string") {
        if (expected === "operating system") {
          const ports = await reservedPorts(count);
          expect(ports, String(listed)).toHaveLength(count);
          for (const port of ports) {
            expect(port).toBeGreaterThan(0);
            expect(port).toBeLessThan(65536);
          }
        } else {
          await expect(reservedPorts(count), String(listed)).rejects.toThrow(expected);
        }
      } else {
        expect(await reservedPorts(count), String(listed)).toEqual(expected);
      }
    }
  });

  it("asks the operating system when the variable is unset", async () => {
    delete process.env["CMSC127_TEST_PORTS"];
    const port = await reservedPort();
    expect(port).toBeGreaterThan(0);
    expect(port).toBeLessThan(65536);
  });
});
