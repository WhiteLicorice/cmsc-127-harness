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
    expect(html).toContain(">1 passed, 0 failed, 0 skipped, 1 warning<");
    expect(html).toContain("section.assistant_id agrees on 639 of 640 rows.");
  });

  it("pluralizes each count in the summary", async () => {
    const { renderChecks } = await import("../../src/panels/checks.js");
    const single = renderChecks({
      results: [{ id: "a", status: "pass" }],
      passed: 1,
      failed: 1,
      skipped: 1,
      warnings: 1,
    });
    expect(single).toContain(">1 passed, 1 failed, 1 skipped, 1 warning<");

    const many = renderChecks({
      results: [
        { id: "a", status: "pass" },
        { id: "b", status: "fail" },
      ],
      passed: 2,
      failed: 3,
      skipped: 0,
      warnings: 2,
    });
    expect(many).toContain(">2 passed, 3 failed, 0 skipped, 2 warnings<");
  });

  it("marks the warning note so the warning colour applies", async () => {
    const { renderChecks } = await import("../../src/panels/checks.js");
    const html = renderChecks({
      results: [
        { id: "c", status: "pass", warnings: ["warning: one row differs."] },
      ],
      passed: 1,
      failed: 0,
      skipped: 0,
      warnings: 1,
    });
    expect(html).toContain('class="warning-note"');
  });
});
