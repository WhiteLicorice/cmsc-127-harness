import { afterEach, expect, it, vi } from "vitest";

class ElementStub {
  textContent = "";
  hidden = false;
  disabled = false;
  value = "public";
  dataset: Record<string, string> = {};
  innerHTML = "";
  children: ElementStub[] = [];
  listeners: Record<string, () => void> = {};
  setAttribute(): void {}
  addEventListener(event: string, callback: () => void): void { this.listeners[event] = callback; }
  replaceChildren(...children: ElementStub[]): void { this.children = children; }
  querySelectorAll(selector: string): ElementStub[] { return selector === "button" ? this.children : []; }
}

afterEach(() => vi.unstubAllGlobals());

it("successful Checks and Tables requests clear only their own error stream", async () => {
  vi.resetModules();
  const elements = new Map(["#panels", "#panel-content", "#schema-select", "#connection-message", "#poll-summary"]
    .map((selector) => [selector, new ElementStub()]));
  const intervals: Array<() => void> = [];
  let tablesFail = false;
  let checksFail = true;
  vi.stubGlobal("document", {
    querySelector: (selector: string) => elements.get(selector),
    createElement: () => new ElementStub(),
    activeElement: null,
  });
  vi.stubGlobal("window", { setInterval: (callback: () => void) => intervals.push(callback) });
  vi.stubGlobal("fetch", async (url: string) => {
    const failure = url.startsWith("/api/tables") ? tablesFail : url === "/api/checks" && checksFail;
    const data = url === "/api/changes" ? { pollIntervalMs: 1000 }
      : url.startsWith("/api/tables") ? { schema: "public", schemas: ["public"], tables: [] }
      : { results: [] };
    return {
      ok: !failure,
      json: async () => failure ? { message: url.startsWith("/api/tables") ? "Tables failed" : "Checks failed" } : data,
    };
  });
  const settle = async (): Promise<void> => { for (let step = 0; step < 20; step += 1) await Promise.resolve(); };
  const tick = async (index: number): Promise<void> => { intervals[index]?.(); await settle(); };
  await import("../../src/app.js");
  await settle();
  const message = elements.get("#connection-message")!;
  elements.get("#panels")!.children.find((button) => button.textContent === "Checks")!.listeners["click"]?.();
  await settle();
  expect(message.textContent).toBe("Checks failed");
  await tick(0);
  expect(message.textContent).toBe("Checks failed");
  checksFail = false;
  await tick(1);
  expect(message.hidden).toBe(true);
  tablesFail = true;
  await tick(0);
  expect(message.textContent).toBe("Tables failed");
  await tick(1);
  expect(message.textContent).toBe("Tables failed");
  tablesFail = false;
  await tick(0);
  expect(message.hidden).toBe(true);
});
