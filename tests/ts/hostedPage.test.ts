import { describe, expect, it } from "vitest";

describe("hosted page placement", () => {
  it("recognizes the Pages origin and nothing else", async () => {
    const { HOSTED_ORIGIN, isHostedPage } = await import("../../src/hosted.js");
    expect(HOSTED_ORIGIN).toBe("https://whitelicorice.github.io");
    expect(isHostedPage(new URL("https://whitelicorice.github.io/cmsc-127-harness/"))).toBe(true);
    expect(isHostedPage(new URL("http://127.0.0.1:55440/"))).toBe(false);
    expect(isHostedPage(new URL("http://localhost:55440/"))).toBe(false);
    expect(isHostedPage(new URL("https://example.com/"))).toBe(false);
  });

  it("keeps a local request relative", async () => {
    const { serverAddress, apiUrl } = await import("../../src/hosted.js");
    const local = new URL("http://127.0.0.1:55440/");
    expect(serverAddress(local)).toBeNull();
    expect(apiUrl("/api/status", serverAddress(local))).toBe("/api/status");
  });

  it("prefixes a hosted request with the server address", async () => {
    const { serverAddress, apiUrl } = await import("../../src/hosted.js");
    const hosted = new URL("https://whitelicorice.github.io/cmsc-127-harness/");
    expect(serverAddress(hosted)).toBe("http://127.0.0.1:55440");
    expect(apiUrl("/api/status", serverAddress(hosted))).toBe("http://127.0.0.1:55440/api/status");
  });

  it("reads the server port from the page address", async () => {
    const { addressFromSearch } = await import("../../src/hosted.js");
    expect(addressFromSearch("")).toBe("http://127.0.0.1:55440");
    expect(addressFromSearch("?port=55443")).toBe("http://127.0.0.1:55443");
    expect(addressFromSearch("?port=0")).toBe("http://127.0.0.1:55440");
    expect(addressFromSearch("?port=70000")).toBe("http://127.0.0.1:55440");
    expect(addressFromSearch("?port=abc")).toBe("http://127.0.0.1:55440");
  });

  it("sends the loopback address space only from the hosted page", async () => {
    const { loopbackInit } = await import("../../src/hosted.js");
    expect(loopbackInit(null)).toEqual({ headers: { Accept: "application/json" } });
    expect(loopbackInit("http://127.0.0.1:55440")).toEqual({
      headers: { Accept: "application/json" },
      targetAddressSpace: "loopback",
    });
  });

  it("accepts a server with the version field and rejects an older one", async () => {
    const { apiVersionOf, versionSupported } = await import("../../src/hosted.js");
    expect(apiVersionOf({ server: "running", api_version: 1 })).toBe(1);
    expect(versionSupported({ server: "running", api_version: 1 })).toBe(true);
    expect(versionSupported({ server: "running", api_version: 2 })).toBe(true);
    expect(apiVersionOf({ server: "running" })).toBe(0);
    expect(versionSupported({ server: "running" })).toBe(false);
    expect(versionSupported({ api_version: "1" })).toBe(false);
    expect(versionSupported({ api_version: 0 })).toBe(false);
    expect(versionSupported(null)).toBe(false);
  });

  it("states the three hosted messages word for word", async () => {
    const { CONNECT_PROMPT, TOO_OLD_MESSAGE, unreachableMessage } =
      await import("../../src/hosted.js");
    expect(CONNECT_PROMPT).toBe(
      "Start the harness server on your computer, then click Connect.",
    );
    expect(TOO_OLD_MESSAGE).toBe(
      "This harness server is too old for the hosted page. Use the local page that the server prints.",
    );
    expect(unreachableMessage("http://127.0.0.1:55440")).toBe(
      "The page can't reach the harness server at http://127.0.0.1:55440. The hosted page requires harness server v1.33 or later. For an earlier release, open the local address that the server prints. If the browser asks for permission to connect to your computer, allow it.",
    );
  });

  it("states the prompt that points another panel to Status", async () => {
    const { CONNECT_IN_STATUS_PROMPT } = await import("../../src/hosted.js");
    expect(CONNECT_IN_STATUS_PROMPT).toBe(
      "Start the harness server on your computer, then open the Status panel and click Connect.",
    );
    expect(CONNECT_IN_STATUS_PROMPT).not.toBe("Start the harness server on your computer, then click Connect.");
  });

  it("names no cause when the browser withholds the status body", async () => {
    // Chrome and Firefox withhold the body of a server that sends no CORS
    // header, which is v1.32 and earlier. The page cannot tell that server
    // from a stopped one, so the message states the release rule and the
    // local route and leaves the cause open.
    const { unreachableMessage } = await import("../../src/hosted.js");
    const message = unreachableMessage("http://127.0.0.1:55440");
    expect(message).toContain("v1.33 or later");
    expect(message).toContain(
      "open the local address that the server prints",
    );
    expect(message).not.toMatch(/too old|CORS|blocked|refused/i);
    expect(message).not.toContain("Start it");
  });
});

describe("status panel connect control", () => {
  it("shows the prompt and the Connect button before the first click", async () => {
    const { renderStatus } = await import("../../src/panels/status.js");
    const markup = renderStatus(
      { server: "running", databaseStatus: "connected" },
      { hosted: true, connected: false },
    );
    expect(markup).toContain("Start the harness server on your computer, then click Connect.");
    expect(markup).toContain(">Connect</button>");
  });

  it("draws no Connect button on the local page", async () => {
    const { renderStatus } = await import("../../src/panels/status.js");
    const markup = renderStatus(
      { server: "running", databaseStatus: "connected", database: "campus", role: "ada" },
      { hosted: false, connected: false },
    );
    expect(markup).not.toContain("Connect");
    expect(markup).toContain("campus");
  });

  it("shows one plain line for a server older than the minimum", async () => {
    const { renderStatus } = await import("../../src/panels/status.js");
    const markup = renderStatus(
      { server: "running", databaseStatus: "connected" },
      { hosted: true, connected: true, tooOld: true },
    );
    expect(markup).toContain(
      "This harness server is too old for the hosted page. Use the local page that the server prints.",
    );
    expect(markup).not.toContain(">Connect</button>");
  });

  it("renders the Status panel through its registry entry", async () => {
    const { panelRegistry } = await import("../../src/panelRegistry.js");
    const status = panelRegistry.find((panel) => panel.id === "status");
    expect(status?.render).toBeTypeOf("function");
    const markup = status?.render(
      { server: "running", databaseStatus: "connected" },
      { hosted: true, connected: false },
    );
    expect(markup).toContain("Start the harness server on your computer, then click Connect.");
    expect(markup).toContain(">Connect</button>");
  });

  it("shows the offline line only after a verified snapshot", async () => {
    const { renderStatus } = await import("../../src/panels/status.js");
    const status = { server: "running", databaseStatus: "connected" } as const;
    const before = renderStatus(status, { hosted: true, connected: false });
    const after = renderStatus(status, { hosted: true, connected: false, saved: true });
    const local = renderStatus(status, { hosted: false, connected: true, saved: true });

    expect(before).not.toContain("Saved for offline use.");
    expect(after).toContain("Saved for offline use.");
    expect(local).not.toContain("Saved for offline use.");
  });
});
