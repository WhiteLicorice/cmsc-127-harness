import { describe, expect, it } from "vitest";

describe("checks panel", () => {
  it("shows each failed case with expected and actual rows", async () => {
    const { describeChecks } = await import("../../src/panels/checks.js");
    const entries = describeChecks({
      results: [
        {
          id: "case-1",
          kind: "query",
          status: "fail",
          expected: { columns: ["id"], rows: [[1]] },
          actual: { columns: ["id"], rows: [[2]] },
        },
      ],
    });

    expect(entries).toMatchObject([{ id: "case-1", status: "fail" }]);
    expect(JSON.stringify(entries[0])).toContain("expected");
    expect(JSON.stringify(entries[0])).toContain("actual");
  });
});
