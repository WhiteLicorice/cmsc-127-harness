import { describe, expect, it } from "vitest";

describe("status panel", () => {
  it("shows a plain connection error", async () => {
    const { statusMessage } = await import("../../src/panels/status.js");
    expect(statusMessage({ server: "available", database: "down", error: "password authentication failed" }))
      .toContain("Check the database password");
  });
});
