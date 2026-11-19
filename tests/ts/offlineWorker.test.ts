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
  state: { hang?: boolean; offline?: boolean; override?: Record<string, string> } = {},
  manifest: unknown = manifestFor(buildId),
) {
  const calls: FetchCalls = [];
  const bodies = PAGE;
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
        init?.signal?.addEventListener("abort", () => reject(new Error("aborted")));
      });
    }
    const relative = url.href.slice(scope.length);
    if (relative === "offline-manifest.json") {
      return new Response(JSON.stringify(manifest), { headers: { "Content-Type": "application/json" } });
    }
    const body = state.override?.[relative === "" ? "index.html" : relative]
      ?? (relative === "" ? bodies["index.html"] : bodies[relative]);
    if (body === undefined) return new Response("missing", { status: 404 });
    return new Response(body, { headers: { "Content-Type": "text/plain" } });
  };
  return { fetch, calls };
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

  async keys(): Promise<string[]> {
    return [...this.entries.keys()];
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
}class FakeTimers {
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
  source?: string;
};

function createWorker(options: WorkerOptions) {
  const caches = options.caches ?? new FakeCaches();
  const timers = options.timers ?? new FakeTimers();
  const listeners = new Map<string, (event: Record<string, unknown>) => void>();
  const messages: { type?: string }[] = [];
  const posted: unknown[] = [];
  let claimed = 0;
  let skipped = 0;
  let unregistered = 0;
  const clients = [{ postMessage: (message: unknown) => messages.push(message as { type?: string }) }];
  const self = {
    location: new URL(scope),
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
      matchAll: async () => clients,
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

  function dispatchFetch(request: unknown): Promise<Response> | undefined {
    let answered: Promise<Response> | undefined;
    const listener = listeners.get("fetch");
    if (!listener) throw new Error("The worker has no fetch listener.");
    listener({
      request,
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
    messages,
    posted,
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

  it("does not fetch again when its own snapshot is complete", async () => {
    const build = "d".repeat(64);
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
    expect(worker.messages.map((message) => message.type)).toContain("offline-ready");
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

  it("keeps the network page when the network answers first", async () => {
    const build = "3".repeat(64);
    const state: { override?: Record<string, string> } = {};
    const worker = createWorker({ buildId: build, fetch: siteFetch(build, state).fetch as never });
    await worker.install();

    state.override = { "index.html": "<!doctype html><title>fresh</title>" };
    const response = await worker.dispatchFetch(navigate(scope));

    expect(await response?.text()).toContain("fresh");
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
