import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { runInNewContext } from "node:vm";
import { describe, expect, it } from "vitest";

// The offline worker keeps one immutable snapshot per build. These tests run
// the shipped template in Node, with a fake cache and a fake network.

const scope = "https://whitelicorice.github.io/cmsc-127-harness/";
const root = resolve(import.meta.dirname, "../..");
const template = readFileSync(resolve(root, "src/service-worker.template.js"), "utf-8");
const killSwitch = readFileSync(resolve(root, "src/service-worker.kill-switch.template.js"), "utf-8");
const encoder = new TextEncoder();

const PAGE: Record<string, string> = {
  "index.html": "<!doctype html><title>harness</title>",
  "style.css": "body { margin: 0; }",
  "app.js": "console.log('page');",
  "vendor/mermaid.min.js": "// mermaid",
};

function sha256(text: string): string {
  return createHash("sha256").update(encoder.encode(text)).digest("hex");
}

function manifestFor(buildId: string, bodies = PAGE, broken: Record<string, string> = {}) {
  return {
    schemaVersion: 1,
    buildId,
    files: Object.keys(bodies).sort().map((path) => ({
      path,
      hash: broken[path] ?? sha256(bodies[path] ?? ""),
    })),
  };
}

type FetchCalls = { path: string; cache: string | null }[];

function siteFetch(
  buildId: string,
  state: {
    hang?: boolean;
    offline?: boolean;
    override?: Record<string, string>;
    httpCache?: Record<string, string>;
    markNetwork?: boolean;
    bodies?: Record<string, string>;
    stallBody?: boolean;
    hangPath?: string;
    resume?: () => void;
  } = {},
  manifest: unknown = manifestFor(buildId),
) {
  const calls: FetchCalls = [];
  const bodies = state.bodies ?? PAGE;
  const fetch = async (
    input: { url?: string; cache?: string } | string,
    init?: { signal?: AbortSignal },
  ): Promise<Response> => {
    const url = new URL(typeof input === "string" ? input : input.url ?? scope);
    const cache = typeof input === "string" ? null : input.cache ?? null;
    calls.push({ path: url.pathname, cache });
    if (state.offline) throw new Error("network down");
    if (state.hang) {
      return await new Promise<Response>((_resolve, reject) => {
        if (init?.signal?.aborted) reject(new Error("aborted"));
        init?.signal?.addEventListener("abort", () => reject(new Error("aborted")));
      });
    }
    const relative = url.href.slice(scope.length);
    // A held file keeps the install inside its download window.
    if (state.hangPath !== undefined && relative === state.hangPath) {
      await new Promise<void>((resume) => {
        state.resume = resume;
      });
    }
    // The body stalls after the headers, as a slow server does. The first bytes
    // arrive and the rest never does.
    if (state.stallBody && (relative === "" || relative === "index.html")) {
      return new Response(new ReadableStream({
        start(controller) {
          controller.enqueue(encoder.encode("<!doctype html>"));
        },
      }), { headers: { "Content-Type": "text/html" } });
    }
    if (relative === "offline-manifest.json") {
      return new Response(JSON.stringify(manifest), { headers: { "Content-Type": "application/json" } });
    }
    // The browser HTTP cache answers any request that does not bypass it.
    if (cache !== "no-store" && cache !== "reload" && state.httpCache?.[relative] !== undefined) {
      return new Response(state.httpCache[relative], { headers: { "Content-Type": "text/plain" } });
    }
    const body = state.override?.[relative === "" ? "index.html" : relative]
      ?? (relative === "" ? bodies["index.html"] : bodies[relative]);
    if (body === undefined) return new Response("missing", { status: 404 });
    // A marked answer tells a test that the worker passed this network body
    // through instead of serving one from its cache.
    const type = relative === "" || relative === "index.html" ? "text/html" : "text/plain";
    const headers: Record<string, string> = { "Content-Type": type };
    if (state.markNetwork) headers["X-Answer"] = "network";
    return new Response(body, { headers });
  };
  return { fetch, calls, state };
}

function cacheKey(key: string | { url: string }): string {
  return typeof key === "string" ? key : key.url;
}

class FakeCache {
  readonly entries = new Map<string, Response>();

  constructor(readonly name: string) {}

  async match(key: string | { url: string }): Promise<Response | undefined> {
    const response = this.entries.get(cacheKey(key));
    return response ? response.clone() : undefined;
  }

  async put(key: string | { url: string }, response: Response): Promise<void> {
    this.entries.set(cacheKey(key), response.clone());
  }

  async delete(key: string | { url: string }): Promise<boolean> {
    return this.entries.delete(cacheKey(key));
  }

  async keys(): Promise<Request[]> {
    return [...this.entries.keys()].map((key) => new Request(key));
  }
}

class FakeCaches {
  readonly caches = new Map<string, FakeCache>();

  async open(name: string): Promise<FakeCache> {
    const cache = this.caches.get(name) ?? new FakeCache(name);
    this.caches.set(name, cache);
    return cache;
  }

  async delete(name: string): Promise<boolean> {
    return this.caches.delete(name);
  }

  async keys(): Promise<string[]> {
    return [...this.caches.keys()];
  }

  async has(name: string): Promise<boolean> {
    return this.caches.has(name);
  }

  // A named read answers from that cache only, and it never creates one.
  async match(
    key: string | { url: string },
    options?: { cacheName?: string },
  ): Promise<Response | undefined> {
    const name = options?.cacheName;
    if (name !== undefined) return this.caches.get(name)?.match(key);
    for (const cache of this.caches.values()) {
      const found = await cache.match(key);
      if (found !== undefined) return found;
    }
    return undefined;
  }
}

// The lock API of a worker. A test can hold a name before a step asks for it.
class FakeLocks {
  private readonly held = new Set<string>();

  hold(name: string): void {
    this.held.add(name);
  }

  release(name: string): void {
    this.held.delete(name);
  }

  async request(
    name: string,
    optionsOrStep: { ifAvailable?: boolean } | ((lock: unknown) => Promise<unknown>),
    maybeStep?: (lock: unknown) => Promise<unknown>,
  ): Promise<unknown> {
    const options = typeof optionsOrStep === "function" ? {} : optionsOrStep;
    const step = (typeof optionsOrStep === "function" ? optionsOrStep : maybeStep) as
      (lock: unknown) => Promise<unknown>;
    if (options.ifAvailable && this.held.has(name)) return step(null);
    this.held.add(name);
    try {
      return await step({ name });
    } finally {
      this.held.delete(name);
    }
  }
}

type Gate = { wait: Promise<void>; open: () => void };

function gate(): Gate {
  let open: () => void = () => undefined;
  const wait = new Promise<void>((resolve) => {
    open = resolve;
  });
  return { wait, open };
}

// A cache list that reports the names it held before a hook runs. A hook can
// delete a snapshot or start an install in that window.
class ScanningCaches extends FakeCaches {
  onScan: ((stack: string) => Promise<void>) | undefined;
  onOpen: ((name: string, stack: string) => Promise<void>) | undefined;
  onHas: ((name: string, stack: string) => Promise<void>) | undefined;

  override async keys(): Promise<string[]> {
    const names = [...this.caches.keys()];
    await this.onScan?.(new Error().stack ?? "");
    return names;
  }

  override async open(name: string): Promise<FakeCache> {
    await this.onOpen?.(name, new Error().stack ?? "");
    return super.open(name);
  }

  // The hook runs after the answer is known and before the caller acts on it.
  // A test can delete the snapshot in that window. The stack comes from the
  // caller, so the hook can tell which step asked.
  override async has(name: string): Promise<boolean> {
    const stack = new Error().stack ?? "";
    const answer = await super.has(name);
    await this.onHas?.(name, stack);
    return answer;
  }
}

// A snapshot that holds its record write until the test releases it. The test
// then chooses where the cleanup meets that write.
class GatedCache extends FakeCache {
  recordWrite: Gate | undefined;
  recordRelease: Gate | undefined;
  recordStored: Gate | undefined;
  onRecordRead: (() => Promise<void>) | undefined;

  override async put(key: string | { url: string }, response: Response): Promise<void> {
    const release = this.recordRelease;
    if (release === undefined || !cacheKey(key).endsWith("/__building__")) {
      await super.put(key, response);
      return;
    }
    this.recordRelease = undefined;
    this.recordWrite?.open();
    await release.wait;
    await super.put(key, response);
    this.recordStored?.open();
  }

  override async match(key: string | { url: string }): Promise<Response | undefined> {
    const answer = await super.match(key);
    const hook = this.onRecordRead;
    if (hook !== undefined && cacheKey(key).endsWith("/__building__")) {
      this.onRecordRead = undefined;
      await hook();
    }
    return answer;
  }
}

// Hands every new snapshot to the test, which sets its gates before the first
// write reaches them.
class GatedCaches extends ScanningCaches {
  prepare: ((cache: GatedCache) => void) | undefined;

  override async open(name: string): Promise<GatedCache> {
    const known = this.caches.get(name);
    if (known !== undefined) return known as GatedCache;
    const cache = new GatedCache(name);
    this.caches.set(name, cache);
    this.prepare?.(cache);
    return cache;
  }
}

class FakeTimers {
  private queue: { id: number; at: number; run: () => void }[] = [];
  private next = 0;
  private now = 0;

  setTimeout = (run: () => void, delay: number): number => {
    const id = (this.next += 1);
    this.queue.push({ id, at: this.now + delay, run });
    return id;
  };

  clearTimeout = (id: number): void => {
    this.queue = this.queue.filter((timer) => timer.id !== id);
  };

  async advance(delay: number): Promise<void> {
    this.now += delay;
    const due = this.queue.filter((timer) => timer.at <= this.now);
    this.queue = this.queue.filter((timer) => timer.at > this.now);
    due.forEach((timer) => timer.run());
    await flush();
  }
}

async function flush(): Promise<void> {
  for (let turn = 0; turn < 20; turn += 1) {
    await new Promise((done) => setImmediate(done));
  }
}

type WorkerOptions = {
  buildId: string;
  fetch: (input: never, init?: never) => Promise<Response>;
  caches?: FakeCaches;
  timers?: FakeTimers;
  locks?: FakeLocks;
  source?: string;
  clientIds?: string[];
};

function createWorker(options: WorkerOptions) {
  const caches = options.caches ?? new FakeCaches();
  const timers = options.timers ?? new FakeTimers();
  const listeners = new Map<string, (event: Record<string, unknown>) => void>();
  const pending: unknown[] = [];
  let claimed = 0;
  let skipped = 0;
  let unregistered = 0;
  let live = [...(options.clientIds ?? [])];
  const self = {
    location: new URL(scope),
    navigator: options.locks === undefined ? {} : { locks: options.locks },
    registration: {
      scope,
      unregister: async () => {
        unregistered += 1;
        return true;
      },
    },
    clients: {
      claim: async () => {
        claimed += 1;
      },
      matchAll: async () => live.map((id) => ({ id })),
    },
    skipWaiting: async () => {
      skipped += 1;
    },
    addEventListener: (type: string, listener: (event: Record<string, unknown>) => void) => {
      listeners.set(type, listener);
    },
  };
  const context = {
    self,
    caches,
    fetch: options.fetch,
    Request,
    Response,
    URL,
    AbortController,
    crypto,
    console,
    setTimeout: timers.setTimeout,
    clearTimeout: timers.clearTimeout,
  };
  const source = (options.source ?? template).replace(
    "\"__OFFLINE_BUILD_ID__\"",
    JSON.stringify(options.buildId),
  );
  runInNewContext(source, context);

  async function dispatch(type: string, data: Record<string, unknown> = {}): Promise<unknown[]> {
    const waits: unknown[] = [];
    const event = {
      ...data,
      waitUntil: (promise: unknown) => waits.push(promise),
    };
    const listener = listeners.get(type);
    if (!listener) throw new Error(`The worker has no ${type} listener.`);
    listener(event);
    await Promise.all(waits);
    return waits;
  }

  function dispatchFetch(request: unknown, extra: Record<string, unknown> = {}): Promise<Response> | undefined {
    let answered: Promise<Response> | undefined;
    const listener = listeners.get("fetch");
    if (!listener) throw new Error("The worker has no fetch listener.");
    listener({
      request,
      clientId: "",
      ...extra,
      waitUntil: (promise: unknown) => {
        pending.push(promise);
      },
      respondWith: (response: Promise<Response>) => {
        answered = response;
      },
    });
    return answered;
  }

  return {
    caches,
    timers,
    dispatch,
    dispatchFetch,
    // Wait for the work that a fetch event handed to waitUntil.
    async settle(): Promise<void> {
      await Promise.all(pending);
      pending.length = 0;
    },
    install: () => dispatch("install"),
    activate: () => dispatch("activate"),
    get skipped() {
      return skipped;
    },
    get claimed() {
      return claimed;
    },
    get unregistered() {
      return unregistered;
    },
    setLiveClients(ids: string[]) {
      live = [...ids];
    },
    async names(): Promise<string[]> {
      return caches.keys();
    },
  };
}

function navigate(url: string) {
  return { url, method: "GET", mode: "navigate", cache: "default" };
}

describe("offline worker install", () => {
  it("builds one immutable cache for its build from a verified manifest", async () => {
    const build = "a".repeat(64);
    const site = siteFetch(build);
    const worker = createWorker({ buildId: build, fetch: site.fetch as never });

    await worker.install();

    expect(await worker.names()).toEqual([`cmsc127-harness-${build}`]);
    expect(worker.skipped).toBe(1);
    expect(site.calls[0]).toEqual({
      path: "/cmsc-127-harness/offline-manifest.json",
      cache: "no-store",
    });
    expect(site.calls.map((call) => call.path)).toContain("/cmsc-127-harness/vendor/mermaid.min.js");
    const cache = await worker.caches.open(`cmsc127-harness-${build}`);
    expect(await cache.match(`${scope}index.html`)).toBeDefined();
    expect(await cache.match(`${scope}offline-manifest.json`)).toBeDefined();
  });

  it("installs past a stale HTTP cache entry for a page file", async () => {
    const build = "a".repeat(64);
    const site = siteFetch(build, { httpCache: { "app.js": "console.log('old page');" } });
    const worker = createWorker({ buildId: build, fetch: site.fetch as never });

    await worker.install();

    expect(worker.skipped).toBe(1);
    const cache = await worker.caches.open(`cmsc127-harness-${build}`);
    expect(await (await cache.match(`${scope}app.js`))?.text()).toBe(PAGE["app.js"]);
    const fileCalls = site.calls.filter((call) => call.path !== "/cmsc-127-harness/offline-manifest.json");
    expect(fileCalls.every((call) => call.cache === "no-store")).toBe(true);
  });

  it("fails a wrong hash and keeps the previous snapshot", async () => {
    const previous = "b".repeat(64);
    const next = "c".repeat(64);
    const caches = new FakeCaches();
    const seed = createWorker({ buildId: previous, fetch: siteFetch(previous).fetch as never, caches });
    await seed.install();
    const previousCache = await caches.open(`cmsc127-harness-${previous}`);
    await previousCache.put(`${scope}marker.txt`, new Response("previous"));

    const broken = manifestFor(next, PAGE, { "app.js": "0".repeat(64) });
    const worker = createWorker({
      buildId: next,
      fetch: siteFetch(next, {}, broken).fetch as never,
      caches,
    });

    await expect(worker.install()).rejects.toThrow(/hash/i);

    expect(await worker.names()).toEqual([`cmsc127-harness-${previous}`]);
    expect(worker.skipped).toBe(0);
    expect(await previousCache.match(`${scope}marker.txt`)).toBeDefined();
  });

  it("does not fetch again when its own snapshot is complete", async () => {    const build = "d".repeat(64);
    const caches = new FakeCaches();
    const first = siteFetch(build);
    await createWorker({ buildId: build, fetch: first.fetch as never, caches }).install();

    const second = siteFetch(build);
    const worker = createWorker({ buildId: build, fetch: second.fetch as never, caches });
    await worker.install();

    // A complete snapshot costs one request for the manifest and no file fetch.
    expect(second.calls.map((call) => call.path)).toEqual(["/cmsc-127-harness/offline-manifest.json"]);
    expect(worker.skipped).toBe(1);
  });
});

describe("offline worker activation", () => {
  it("deletes older snapshots only, then claims the page", async () => {
    const build = "e".repeat(64);
    const caches = new FakeCaches();
    await (await caches.open(`cmsc127-harness-${"1".repeat(64)}`)).put(`${scope}index.html`, new Response("old"));
    await (await caches.open("ren-courses-offline-old")).put(`${scope}index.html`, new Response("other"));
    await (await caches.open(`cmsc127-harness-${build}`)).put(`${scope}index.html`, new Response("new"));
    const worker = createWorker({ buildId: build, fetch: siteFetch(build).fetch as never, caches });

    await worker.activate();

    expect((await worker.names()).sort()).toEqual([`cmsc127-harness-${build}`, "ren-courses-offline-old"].sort());
    expect(worker.claimed).toBe(1);
  });

  it("keeps a snapshot that another install still fills", async () => {
    // A reload runs the active worker's cleanup while a new build downloads.
    // The new snapshot carries a fresh building record, so the cleanup keeps it.
    const active = "e".repeat(64);
    const next = "f".repeat(64);
    const caches = new FakeCaches();
    const first = createWorker({ buildId: active, fetch: siteFetch(active).fetch as never, caches });
    await first.install();

    const site = siteFetch(next, { hangPath: "index.html" });
    const worker = createWorker({ buildId: next, fetch: site.fetch as never, caches });
    const installing = worker.install();
    // Wait for the install to write its record and reach the held file.
    for (let turn = 0; turn < 200 && site.state.resume === undefined; turn += 1) await flush();
    expect(site.state.resume).toBeDefined();

    await first.activate();

    expect(await first.names()).toContain(`cmsc127-harness-${next}`);
    site.state.resume?.();
    await installing;
    expect(await first.names()).toContain(`cmsc127-harness-${next}`);
  });

  it("deletes a snapshot whose building record is stale", async () => {
    // An install that stopped leaves its record behind. The limit ages it out,
    // so a later cleanup can still remove the half-filled snapshot.
    const active = "a".repeat(64);
    const stale = "b".repeat(64);
    const caches = new FakeCaches();
    const first = createWorker({ buildId: active, fetch: siteFetch(active).fetch as never, caches });
    await first.install();
    const half = await caches.open(`cmsc127-harness-${stale}`);
    await half.put(`${scope}__building__`, new Response(String(Date.now() - 7_200_000)));

    await first.activate();

    expect(await first.names()).toEqual([`cmsc127-harness-${active}`]);
  });

  it("removes a snapshot that carries no manifest and no building record", async () => {
    const active = "c".repeat(64);
    const empty = "d".repeat(64);
    const caches = new FakeCaches();
    const first = createWorker({ buildId: active, fetch: siteFetch(active).fetch as never, caches });
    await first.install();
    await (await caches.open(`cmsc127-harness-${empty}`)).put(`${scope}app.js`, new Response("part"));

    await first.activate();

    expect(await first.names()).toEqual([`cmsc127-harness-${active}`]);
  });

  it("keeps an empty snapshot that an install has just opened", async () => {
    // The install opens its cache before it can write the building record. A
    // cleanup in that window must not delete the snapshot the install holds.
    const active = "1".repeat(64);
    const opening = "2".repeat(64);
    const caches = new FakeCaches();
    const first = createWorker({ buildId: active, fetch: siteFetch(active).fetch as never, caches });
    await first.install();
    await caches.open(`cmsc127-harness-${opening}`);

    await first.activate();

    expect(await first.names()).toContain(`cmsc127-harness-${opening}`);
  });

  it("keeps a snapshot that an install opens after the cache list", async () => {
    // The cleanup reads the cache list, then an install opens its snapshot. A
    // cleanup that reads the list again for its deletes meets a name that it
    // never classified, and removes a live install.
    const active = "5".repeat(64);
    const opening = "6".repeat(64);
    const caches = new ScanningCaches();
    const first = createWorker({ buildId: active, fetch: siteFetch(active).fetch as never, caches });
    await first.install();
    let started = false;
    caches.onScan = async (stack) => {
      if (started || !stack.includes("deleteUnusedSnapshots")) return;
      started = true;
      const cache = await caches.open(`cmsc127-harness-${opening}`);
      await cache.put(`${scope}__building__`, new Response(String(Date.now())));
    };

    await first.activate();

    expect(await first.names()).toContain(`cmsc127-harness-${opening}`);
  });

  it("keeps a snapshot whose record arrives after the cleanup reads it", async () => {
    // The cleanup reads the building record before the install writes it, then
    // reads the cache as non-empty. The snapshot has no manifest yet, so a
    // cleanup that trusts that order deletes a live install.
    const active = "3".repeat(64);
    const next = "4".repeat(64);
    const caches = new GatedCaches();
    const first = createWorker({ buildId: active, fetch: siteFetch(active).fetch as never, caches });
    await first.install();

    const recordWrite = gate();
    const recordRelease = gate();
    const recordStored = gate();
    caches.prepare = (cache) => {
      if (!cache.name.endsWith(next)) return;
      cache.recordWrite = recordWrite;
      cache.recordRelease = recordRelease;
      cache.recordStored = recordStored;
    };
    const worker = createWorker({ buildId: next, fetch: siteFetch(next).fetch as never, caches });
    const installing = worker.install();
    await recordWrite.wait;

    // The record is the first write of the install. The cleanup reads it, then
    // the write lands before the empty check.
    const cache = await caches.open(`cmsc127-harness-${next}`);
    let released = false;
    cache.onRecordRead = async () => {
      released = true;
      recordRelease.open();
      await recordStored.wait;
    };
    await first.activate();
    if (!released) recordRelease.open();
    await recordStored.wait;
    await installing;

    expect(await first.names()).toContain(`cmsc127-harness-${next}`);
  });

  it("skips a snapshot whose lock an install holds", async () => {
    // The lock keeps a delete apart from an install that fills the same cache.
    // A snapshot with files and no manifest looks like a stopped install, so
    // only the lock tells the two apart.
    const active = "9".repeat(64);
    const next = "a".repeat(64);
    const caches = new FakeCaches();
    const locks = new FakeLocks();
    const first = createWorker({
      buildId: active,
      fetch: siteFetch(active).fetch as never,
      caches,
      locks,
    });
    await first.install();
    await (await caches.open(`cmsc127-harness-${next}`)).put(`${scope}app.js`, new Response("part"));
    locks.hold(`cmsc127-harness-${next}-lock`);

    await first.activate();

    expect(await first.names()).toContain(`cmsc127-harness-${next}`);

    locks.release(`cmsc127-harness-${next}-lock`);
    await first.activate();

    expect(await first.names()).toEqual([`cmsc127-harness-${active}`]);
  });

  it("does not plant a snapshot that another worker deletes during a read", async () => {
    // A read opens the snapshot to answer a file. A cleanup in another worker
    // can remove that snapshot first. The read must not create it again.
    const build = "7".repeat(64);
    const caches = new ScanningCaches();
    const worker = createWorker({ buildId: build, fetch: siteFetch(build).fetch as never, caches });
    await worker.install();
    let deleted = false;
    caches.onScan = async (stack) => {
      if (deleted || !stack.includes("buildFor")) return;
      deleted = true;
      await caches.delete(`cmsc127-harness-${build}`);
    };

    const response = await worker.dispatchFetch(new Request(`${scope}app.js`), { clientId: "tab" });

    expect(await response?.text()).toBe(PAGE["app.js"]);
    expect(await worker.names()).toEqual([]);
  });

  it("does not plant a snapshot that another worker deletes during a record delete", async () => {
    // A navigation drops the record of the document it replaces. A cleanup can
    // remove that snapshot first. The record delete must not create it again.
    const build = "8".repeat(64);
    const caches = new ScanningCaches();
    const worker = createWorker({ buildId: build, fetch: siteFetch(build).fetch as never, caches });
    await worker.install();
    let deleted = false;
    caches.onScan = async (stack) => {
      if (deleted || !stack.includes("forgetBuild")) return;
      deleted = true;
      await caches.delete(`cmsc127-harness-${build}`);
    };

    await worker.dispatchFetch(navigate(scope), { clientId: "old", resultingClientId: "new" });
    await worker.settle();

    expect(await worker.names()).toEqual([]);
  });

  it("does not plant a snapshot that another cleanup deletes during the scan", async () => {
    // Two cleanups overlap. The first reads the cache list. The second deletes
    // an expired snapshot under its lock. The first then opens the missing
    // name. An open outside the lock plants an empty cache, and the next pass
    // keeps an empty cache because an install opens its snapshot before it
    // writes its record.
    const active = "d".repeat(64);
    const orphan = "e".repeat(64);
    const caches = new ScanningCaches();
    const locks = new FakeLocks();
    const worker = createWorker({
      buildId: active,
      fetch: siteFetch(active).fetch as never,
      caches,
      locks,
    });
    await worker.install();
    await (await caches.open(`cmsc127-harness-${orphan}`)).put(`${scope}__building__`, new Response("0"));

    const other = createWorker({
      buildId: active,
      fetch: siteFetch(active).fetch as never,
      caches,
      locks,
    });
    let overlapped = false;
    caches.onScan = async (stack) => {
      if (overlapped || !stack.includes("deleteUnusedSnapshots")) return;
      overlapped = true;
      // The second cleanup runs to its end while the first holds its list.
      await other.activate();
    };

    await worker.dispatchFetch(navigate(scope), { clientId: "old", resultingClientId: "new" });
    await worker.settle();

    expect(await worker.names()).toEqual([`cmsc127-harness-${active}`]);
  });

  it("does not plant a snapshot that another cleanup deletes between the record check and the open", async () => {
    // A navigation drops the record of the document it replaces. The record
    // delete checks that the snapshot exists, then opens it. A cleanup in
    // another worker can delete the snapshot between those two steps. The open
    // then plants an empty cache, and the next pass keeps it.
    const build = "f".repeat(64);
    const retired = "1".repeat(64);
    const caches = new ScanningCaches();
    const locks = new FakeLocks();
    const worker = createWorker({
      buildId: build,
      fetch: siteFetch(build).fetch as never,
      caches,
      locks,
    });
    await worker.install();
    const old = await caches.open(`cmsc127-harness-${retired}`);
    await old.put(`${scope}offline-manifest.json`, new Response(JSON.stringify(manifestFor(retired))));

    const other = createWorker({
      buildId: build,
      fetch: siteFetch(build).fetch as never,
      caches,
      locks,
    });
    let overlapped = false;
    caches.onHas = async (name, stack) => {
      if (overlapped || name !== `cmsc127-harness-${retired}` || !stack.includes("forgetBuild")) return;
      overlapped = true;
      // The second cleanup runs to its end between the check and the open.
      await other.activate();
    };

    await worker.dispatchFetch(navigate(scope), { clientId: "old", resultingClientId: "new" });
    await worker.settle();

    expect(await worker.names()).toEqual([`cmsc127-harness-${build}`]);
  });

  it("keeps a snapshot that another install completes while this one fails", async () => {
    // The install reads the cache list before it opens its snapshot. Another
    // worker can complete that snapshot in that window. A failed install must
    // not remove a snapshot that carries a manifest.
    const build = "b".repeat(64);
    const caches = new ScanningCaches();
    let seeded = false;
    caches.onOpen = async (name) => {
      if (seeded || name !== `cmsc127-harness-${build}`) return;
      seeded = true;
      const cache = await caches.open(name);
      await cache.put(`${scope}offline-manifest.json`, new Response(JSON.stringify(manifestFor(build))));
    };
    const broken = manifestFor(build, PAGE, { "app.js": "0".repeat(64) });
    const worker = createWorker({
      buildId: build,
      fetch: siteFetch(build, {}, broken).fetch as never,
      caches,
    });

    await expect(worker.install()).rejects.toThrow(/hash/i);

    expect(await worker.names()).toEqual([`cmsc127-harness-${build}`]);
  });

  it("keeps a complete snapshot when a later install of the same build fails", async () => {
    // A snapshot with a manifest answers documents. A failed install must not
    // remove it, even when this install did not create it.
    const build = "c".repeat(64);
    const caches = new FakeCaches();
    await createWorker({ buildId: build, fetch: siteFetch(build).fetch as never, caches }).install();
    const cache = await caches.open(`cmsc127-harness-${build}`);
    await cache.delete(`${scope}app.js`);
    const broken = manifestFor(build, PAGE, { "app.js": "0".repeat(64) });
    const worker = createWorker({
      buildId: build,
      fetch: siteFetch(build, {}, broken).fetch as never,
      caches,
    });

    await expect(worker.install()).rejects.toThrow(/hash/i);

    expect(await worker.names()).toEqual([`cmsc127-harness-${build}`]);
  });
});

describe("offline worker requests", () => {
  it("answers no API request, no loopback request, and no other origin", () => {
    const build = "f".repeat(64);
    const worker = createWorker({ buildId: build, fetch: siteFetch(build).fetch as never });
    const requests = [
      new Request(`${scope}api/status`),
      new Request(`${scope}api/tables?schema=public`),
      new Request("https://whitelicorice.github.io/api/status"),
      new Request("http://127.0.0.1:55440/api/status"),
      new Request("http://127.0.0.1:55440/app.js"),
      new Request("https://example.com/app.js"),
    ];

    for (const request of requests) {
      expect(worker.dispatchFetch(request)).toBeUndefined();
    }
    expect(worker.dispatchFetch(navigate("https://example.com/page"))).toBeUndefined();
  });

  it("refuses a manifest that names another build", async () => {
    const build = "8".repeat(64);
    const site = siteFetch(build, {}, manifestFor("9".repeat(64)));
    const worker = createWorker({ buildId: build, fetch: site.fetch as never });

    await expect(worker.install()).rejects.toThrow(/does not match the worker/);
    expect(await worker.names()).toEqual([]);
    expect(worker.skipped).toBe(0);
  });

  it("falls back to the cached page after the timeout", async () => {
    const build = "2".repeat(64);
    const state: { hang?: boolean } = { hang: false };
    const site = siteFetch(build, state);
    const timers = new FakeTimers();
    const worker = createWorker({ buildId: build, fetch: site.fetch as never, timers });
    await worker.install();

    state.hang = true;
    const pending = worker.dispatchFetch(navigate(scope));
    expect(pending).toBeDefined();
    await timers.advance(3000);
    const response = await pending;

    expect(await response?.text()).toBe(PAGE["index.html"]);
  });

  it("falls back to the cached page when the body stalls after the headers", async () => {
    // A slow server sends the headers and the first bytes, then stops. The
    // deadline must cover the body read, not only the headers.
    const build = "4".repeat(64);
    const state: { stallBody?: boolean } = { stallBody: false };
    const site = siteFetch(build, state);
    const timers = new FakeTimers();
    const worker = createWorker({ buildId: build, fetch: site.fetch as never, timers });
    await worker.install();

    state.stallBody = true;
    const pending = worker.dispatchFetch(navigate(scope));
    expect(pending).toBeDefined();
    await timers.advance(3000);
    const response = await pending;

    expect(await response?.text()).toBe(PAGE["index.html"]);
  });

  it("keeps the network page when the network answers first", async () => {
    const build = "3".repeat(64);
    const state: { override?: Record<string, string> } = {};
    const worker = createWorker({ buildId: build, fetch: siteFetch(build, state).fetch as never });
    await worker.install();

    // The network serves the same page as the snapshot. The worker must pass
    // that body through instead of answering from the cache.
    state.override = { "index.html": PAGE["index.html"] };
    state.markNetwork = true;
    const response = await worker.dispatchFetch(navigate(scope));

    expect(response?.headers.get("X-Answer")).toBe("network");
    expect(await response?.text()).toBe(PAGE["index.html"]);
  });

  it("serves the active page when the network HTML belongs to another build", async () => {
    // A rejected update keeps the first snapshot. The next navigation then
    // meets HTML from the second build. The worker must answer with the page
    // that matches the scripts in its snapshot, or the page pairs new HTML
    // with old JavaScript.
    const build = "7".repeat(64);
    const state: { override?: Record<string, string>; markNetwork?: boolean } = {};
    const worker = createWorker({ buildId: build, fetch: siteFetch(build, state).fetch as never });
    await worker.install();

    state.override = {
      "index.html": "<!doctype html><title>second build</title><div id=\"panel-b\"></div>",
    };
    state.markNetwork = true;
    for (const target of [scope, `${scope}index.html`]) {
      const response = await worker.dispatchFetch(navigate(target));

      expect(response?.headers.get("X-Answer")).not.toBe("network");
      expect(await response?.text()).toBe(PAGE["index.html"]);
    }
  });

  it("passes the network page through when no snapshot is active", async () => {
    const build = "8".repeat(64);
    const state: { override?: Record<string, string>; markNetwork?: boolean } = {};
    const worker = createWorker({ buildId: build, fetch: siteFetch(build, state).fetch as never });

    state.override = { "index.html": "<!doctype html><title>first load</title>" };
    state.markNetwork = true;
    const response = await worker.dispatchFetch(navigate(scope));

    expect(response?.headers.get("X-Answer")).toBe("network");
    expect(await response?.text()).toContain("first load");
  });

  it("serves a page asset from the snapshot", async () => {    const build = "4".repeat(64);
    const worker = createWorker({ buildId: build, fetch: siteFetch(build).fetch as never });
    await worker.install();

    const response = await worker.dispatchFetch(new Request(`${scope}app.js`));

    expect(await response?.text()).toBe(PAGE["app.js"]);
  });

  it("answers 503 for an asset that is not in the snapshot and not on the network", async () => {
    const build = "5".repeat(64);
    const state: { offline?: boolean } = {};
    const worker = createWorker({ buildId: build, fetch: siteFetch(build, state).fetch as never });
    await worker.install();

    state.offline = true;
    const response = await worker.dispatchFetch(new Request(`${scope}missing.js`));

    expect(response?.status).toBe(503);
  });

  it("reads the snapshot without creating one before install", async () => {
    const build = "6".repeat(64);
    const state: { offline?: boolean } = {};
    const worker = createWorker({ buildId: build, fetch: siteFetch(build, state).fetch as never });

    state.offline = true;
    await worker.dispatchFetch(new Request(`${scope}app.js`));
    await worker.dispatchFetch(navigate(scope));

    expect(await worker.names()).toEqual([]);
  });
});

describe("offline kill switch", () => {
  it("clears only harness caches and unregisters itself", async () => {
    const caches = new FakeCaches();
    await caches.open(`cmsc127-harness-${"6".repeat(64)}`);
    await caches.open(`cmsc127-harness-${"7".repeat(64)}`);
    await caches.open("ren-courses-offline-old");
    const worker = createWorker({
      buildId: "0".repeat(64),
      fetch: siteFetch("0".repeat(64)).fetch as never,
      caches,
      source: killSwitch,
    });

    await worker.install();
    await worker.activate();

    expect(await worker.names()).toEqual(["ren-courses-offline-old"]);
    expect(worker.unregistered).toBe(1);
    expect(worker.claimed).toBe(1);
  });
});
