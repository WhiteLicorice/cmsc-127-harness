import { afterEach, describe, expect, it, vi } from "vitest";

// The local page registers no worker, and the hosted page shows one notice
// when a new build takes over.

type Registration = { scriptURL: string };
type Appended = { textContent: string; className: string; attributes: Record<string, string> };

type PageStub = {
  registered: Registration[];
  appended: Appended[];
  fireControllerChange: () => void;
};

afterEach(() => {
  vi.unstubAllGlobals();
});

function stubPage(origin: string, hasController: boolean): PageStub {
  const registered: Registration[] = [];
  const appended: Appended[] = [];
  const listeners = new Map<string, () => void>();
  vi.stubGlobal("window", { location: new URL(`${origin}/cmsc-127-harness/`) });
  vi.stubGlobal("navigator", {
    serviceWorker: {
      register: async (scriptURL: string) => {
        registered.push({ scriptURL });
        return { scope: `${origin}/cmsc-127-harness/`, state: "installing" };
      },
      addEventListener: (type: string, listener: () => void) => {
        listeners.set(type, listener);
      },
      controller: hasController ? { scriptURL: `${origin}/cmsc-127-harness/service-worker.js` } : null,
      ready: Promise.resolve({ active: null }),
    },
  });
  vi.stubGlobal("document", {
    baseURI: `${origin}/cmsc-127-harness/`,
    body: { append: (node: Appended) => appended.push(node) },
    querySelector: () => null,
    createElement: () => {
      const node = {
        textContent: "",
        className: "",
        attributes: {} as Record<string, string>,
        setAttribute(name: string, value: string) {
          node.attributes[name] = value;
        },
        remove: () => undefined,
      };
      return node;
    },
  });
  return {
    registered,
    appended,
    fireControllerChange: () => listeners.get("controllerchange")?.(),
  };
}

describe("registerOfflineWorker", () => {
  it("does nothing on the local page", async () => {
    vi.resetModules();
    const page = stubPage("http://127.0.0.1:55440", false);
    const { registerOfflineWorker } = await import("../../src/offline.js");

    registerOfflineWorker();
    page.fireControllerChange();

    expect(page.registered).toEqual([]);
    expect(page.appended).toEqual([]);
  });

  it("registers the worker on the hosted page", async () => {
    vi.resetModules();
    const page = stubPage("https://whitelicorice.github.io", false);
    const { registerOfflineWorker } = await import("../../src/offline.js");

    registerOfflineWorker();
    await Promise.resolve();

    expect(page.registered).toEqual([
      { scriptURL: "https://whitelicorice.github.io/cmsc-127-harness/service-worker.js" },
    ]);
  });

  it("does nothing when the browser has no service worker", async () => {
    vi.resetModules();
    stubPage("https://whitelicorice.github.io", false);
    vi.stubGlobal("navigator", {});
    const { registerOfflineWorker } = await import("../../src/offline.js");

    expect(() => registerOfflineWorker()).not.toThrow();
  });

  it("states the new-build notice word for word", async () => {
    vi.resetModules();
    stubPage("https://whitelicorice.github.io", false);
    const { NEW_BUILD_NOTICE } = await import("../../src/offline.js");

    expect(NEW_BUILD_NOTICE).toBe(
      "A new version of this page is ready. It loads the next time you open the page.",
    );
  });

  it("shows the notice when a new worker takes over", async () => {
    vi.resetModules();
    const page = stubPage("https://whitelicorice.github.io", true);
    const { registerOfflineWorker, NEW_BUILD_NOTICE } = await import("../../src/offline.js");
    registerOfflineWorker();

    page.fireControllerChange();

    expect(page.appended).toHaveLength(1);
    expect(page.appended[0]?.textContent).toBe(NEW_BUILD_NOTICE);
  });

  it("shows no notice on the first install", async () => {
    vi.resetModules();
    const page = stubPage("https://whitelicorice.github.io", false);
    const { registerOfflineWorker } = await import("../../src/offline.js");
    registerOfflineWorker();

    page.fireControllerChange();

    expect(page.appended).toEqual([]);
  });
});
