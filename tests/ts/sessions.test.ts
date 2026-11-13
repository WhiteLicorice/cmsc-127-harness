import { describe, expect, it } from "vitest";

describe("sessions panel", () => {
  it("states the blocking session and open transaction", async () => {
    const { describeSession } = await import("../../src/panels/sessions.js");
    const text = describeSession({
      pid: 42,
      role: "student",
      state: "active",
      openTransaction: true,
      blockingPids: [17],
    });

    expect(text).toContain("waits for 17");
    expect(text).toContain("open transaction");
  });

  it("states when a session does not wait", async () => {
    const { describeSession } = await import("../../src/panels/sessions.js");
    const text = describeSession({
      pid: 42,
      role: "student",
      state: "idle",
      openTransaction: false,
      blockingPids: [],
    });

    expect(text).toContain("not waiting");
  });
});
