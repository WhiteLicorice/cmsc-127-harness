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

  it("shows a passing case's warnings apart from its result", async () => {
    const { describeChecks, renderChecks } = await import(
      "../../src/panels/checks.js"
    );
    const result = {
      results: [
        {
          id: "case-2",
          kind: "query",
          status: "pass",
          warnings: ["warning: section.assistant_id agrees on 639 of 640 rows."],
        },
      ],
      passed: 1,
      failed: 0,
      skipped: 0,
      warnings: 1,
    };
    const entries = describeChecks(result);

    expect(entries).toMatchObject([
      { id: "case-2", status: "pass", warnings: [expect.stringContaining("639")] },
    ]);
    const html = renderChecks(result);
    expect(html).toContain("1 passed, 0 failed, 0 skipped, 1 warnings");
    expect(html).toContain("section.assistant_id agrees on 639 of 640 rows.");
  });
});
