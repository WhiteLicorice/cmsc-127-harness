import { describe, expect, it } from "vitest";

describe("schema panel", () => {
  it("builds a live diagram and states the compared difference", async () => {
    const { buildSchemaView } = await import("../../src/panels/schema.js");
    const view = buildSchemaView({
      schema: "public",
      erdText: "erDiagram\n  parent ||--o{ child : owns",
      tables: [
        {
          name: "parent",
          columns: [{ name: "id", type: "integer", notNull: true }],
          primaryKey: ["id"],
          constraints: [{ name: "parent_pkey", type: "PRIMARY KEY", definition: "PRIMARY KEY (id)" }],
        },
        {
          name: "child",
          columns: [{ name: "parent_id", type: "integer", notNull: true }],
          primaryKey: [],
          constraints: [{ name: "child_parent_id_fkey", type: "FOREIGN KEY", definition: "FOREIGN KEY (parent_id) REFERENCES parent(id)" }],
        },
      ],
      relationships: [
        { child: "child", parent: "parent", childColumns: ["parent_id"], parentColumns: ["id"], required: true, unique: false },
      ],
      differences: [{ message: "child is missing column value" }],
    });

    expect(view.liveDiagram).toContain("parent");
    expect(view.relationships).toContain("child: exactly one parent. parent: zero or many child.");
    expect(view.differences).toEqual(["child is missing column value"]);
    expect(view.constraints.some((item) => item.includes("FOREIGN KEY (parent_id) REFERENCES parent(id)"))).toBe(true);
  });
});
