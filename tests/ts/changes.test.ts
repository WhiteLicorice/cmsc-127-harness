import { describe, expect, it } from "vitest";

describe("change feed", () => {
  it("derives time-stamped events from table snapshots", async () => {
    const { buildFeed } = await import("../../src/panels/changes.js");
    const events = buildFeed(
      [{ table: "item", primaryKey: ["id"], rows: [{ id: 1, value: "old" }] }],
      [{ table: "item", primaryKey: ["id"], rows: [{ id: 1, value: "new" }] }],
      "2026-11-13T00:00:00.000Z",
    );

    expect(events).toMatchObject([{ table: "item", kind: "update", time: "2026-11-13T00:00:00.000Z" }]);
  });

  it("orders events by observed time", async () => {
    const { orderFeed } = await import("../../src/panels/changes.js");
    const ordered = orderFeed([
      { table: "item", kind: "insert", key: "2", time: "2026-11-13T00:00:02.000Z" },
      { table: "item", kind: "delete", key: "1", time: "2026-11-13T00:00:01.000Z" },
    ]);

    expect(ordered.map((event) => event.key)).toEqual(["1", "2"]);
  });
});
