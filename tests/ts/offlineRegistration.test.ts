import { afterEach, describe, expect, it, vi } from "vitest";

// The local page registers no worker. This test states that rule.

type Registration = { scriptURL: string };

afterEach(() => {
  vi.unstubAllGlobals();
  vi.doUnmock("../../src/hosted.js");
});

function stubPage(origin: string): { registered: Registration[]; appended: unknown[] } {
  const registered: Registration[] = [];
  const appended: unknown[] = [];
  vi.stubGlobal("window", { location: new URL(`${origin}/cmsc-127-harness/`) });
  vi.stubGlobal("navigator", {
    serviceWorker: {
      register: async (scriptURL: string) => {
        registered.push({ scriptURL });
        return { scope: `${origin}/cmsc-127-harness/`, state: "installing" };
      },
      addEventListener: () => undefined,
      controller: null,
      ready: Promise.resolve({ active: null }),
    },
  });
  vi.stubGlobal("document", {
    baseURI: `${origin}/cmsc-127-harness/`,
    body: { append: (node: unknown) => appended.push(node) },
    querySelector: () => null,
    createElement: () => ({
      textContent: "",
      setAttribute: () => undefined,
      remove: () => undefined,
    }),
  });
  return { registered, appended };
}

describe("registerOfflineWorker", () => {
  it("does nothing on the local page", async () => {
    vi.resetModules();
    const { registered, appended } = stubPage("http://127.0.0.1:55440");
    const { registerOfflineWorker } = await import("../../src/offline.js");

    registerOfflineWorker();

    expect(registered).toEqual([]);
    expect(appended).toEqual([]);
  });

  it("registers the worker on the hosted page", async () => {
    vi.resetModules();
    const { registered } = stubPage("https://whitelicorice.github.io");
    const { registerOfflineWorker } = await import("../../src/offline.js");

    registerOfflineWorker();
    await Promise.resolve();

    expect(registered).toEqual([
      { scriptURL: "https://whitelicorice.github.io/cmsc-127-harness/service-worker.js" },
    ]);
  });

  it("does nothing when the browser has no service worker", async () => {
    vi.resetModules();
    stubPage("https://whitelicorice.github.io");
    vi.stubGlobal("navigator", {});
    const { registerOfflineWorker } = await import("../../src/offline.js");

    expect(() => registerOfflineWorker()).not.toThrow();
  });

  it("states the new-build notice word for word", async () => {
    vi.resetModules();
    stubPage("https://whitelicorice.github.io");
    const { NEW_BUILD_NOTICE } = await import("../../src/offline.js");

    expect(NEW_BUILD_NOTICE).toBe(
      "A new version of this page is ready. It loads the next time you open the page.",
    );
  });
});
