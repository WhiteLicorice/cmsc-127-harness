import { describe, expect, it } from "vitest";

describe("row comparison", () => {
  it("marks an inserted row", async () => {
    const { compareRows } = await import("../../src/panels/tables.js");
    const changes = compareRows(
      { table: "item", primaryKey: ["id"], rows: [] },
      { table: "item", primaryKey: ["id"], rows: [{ id: 1, value: "a" }] },
    );

    expect(changes.map((change) => change.kind)).toEqual(["insert"]);
  });

  it("marks a row whose non-key value changed, including null", async () => {
    const { compareRows } = await import("../../src/panels/tables.js");
    const changes = compareRows(
      { table: "item", primaryKey: ["id"], rows: [{ id: 1, value: null }] },
      { table: "item", primaryKey: ["id"], rows: [{ id: 1, value: "set" }] },
    );

    expect(changes.map((change) => change.kind)).toEqual(["update"]);
    expect(changes[0]?.previous?.value).toBeNull();
  });

  it("marks a deleted row", async () => {
    const { compareRows } = await import("../../src/panels/tables.js");
    const changes = compareRows(
      { table: "item", primaryKey: ["id"], rows: [{ id: 1, value: "a" }] },
      { table: "item", primaryKey: ["id"], rows: [] },
    );

    expect(changes.map((change) => change.kind)).toEqual(["delete"]);
  });

  it("compares full rows when the table has no primary key", async () => {
    const { compareRows } = await import("../../src/panels/tables.js");
    const changes = compareRows(
      { table: "item", primaryKey: [], rows: [{ label: "a", value: null }] },
      { table: "item", primaryKey: [], rows: [{ label: "a", value: "set" }] },
    );

    expect(changes.map((change) => change.kind).sort()).toEqual(["delete", "insert"]);
  });

  it("treats a changed primary key as a delete and an insert", async () => {
    const { compareRows } = await import("../../src/panels/tables.js");
    const changes = compareRows(
      { table: "item", primaryKey: ["id"], rows: [{ id: 1, value: "a" }] },
      { table: "item", primaryKey: ["id"], rows: [{ id: 2, value: "a" }] },
    );

    expect(changes.map((change) => change.kind).sort()).toEqual(["delete", "insert"]);
  });

  it("keeps duplicate rows by their count when there is no primary key", async () => {
    const { compareRows } = await import("../../src/panels/tables.js");
    const changes = compareRows(
      { table: "item", primaryKey: [], rows: [{ value: 1 }, { value: 1 }] },
      { table: "item", primaryKey: [], rows: [{ value: 1 }] },
    );

    expect(changes.map((change) => change.kind)).toEqual(["delete"]);
  });
});
