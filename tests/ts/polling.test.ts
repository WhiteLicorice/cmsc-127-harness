import { describe, expect, it } from "vitest";

describe("poll settings", () => {
  it("falls back to the default interval when the settings request fails", async () => {
    const { readPollSettings } = await import("../../src/polling.js");
    const error = new Error("connection reset");

    const settings = await readPollSettings(async () => {
      throw error;
    });

    expect(settings.intervalMs).toBe(1000);
    expect(settings.error).toBe(error);
  });

  it("keeps the interval the server reports", async () => {
    const { readPollSettings } = await import("../../src/polling.js");

    const settings = await readPollSettings(async () => ({ pollIntervalMs: 250 }));

    expect(settings.intervalMs).toBe(250);
    expect(settings.error).toBeNull();
  });
});
