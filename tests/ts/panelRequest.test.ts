import { describe, expect, it } from "vitest";

describe("panel request selection", () => {
  it("accepts a response for the panel and schema that remain selected", async () => {
    const { selectionMatches } = await import("../../src/panelRequest.js");

    expect(selectionMatches("schema", "schema", "public", "public")).toBe(true);
  });

  it("rejects a response after the panel or schema changes", async () => {
    const { selectionMatches } = await import("../../src/panelRequest.js");

    expect(selectionMatches("schema", "roles", "public", "public")).toBe(false);
    expect(selectionMatches("schema", "schema", "public", "lab")).toBe(false);
  });
});
